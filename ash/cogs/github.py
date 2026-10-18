"""GitHub Webhook 受信: Issue / PR / リリース / コミットを Discord へ通知する。

送信先の振り分け:
    Issue / PR 関連 : issue_routes（番号別）→ issue_channel_id → default_channel_id
    リリース        : release_channel_id → default_channel_id
    コミット（push）: commit_channel_id → default_channel_id
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

import discord
from aiohttp import web
from discord.ext import commands

from ash.bot import AshBot
from ash.notifier import make_embed
from ash.utils import truncate

log = logging.getLogger(__name__)

# 重複配信の判定に覚えておく Delivery ID の件数
SEEN_DELIVERY_LIMIT = 500
# events 未設定時に通知するイベント
DEFAULT_EVENTS = {"issue_comment": True, "issues": True, "release": True}
# Issue / PR テンプレートに含まれる HTML コメント
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# イベント種別ごとの埋め込み色
COLOR_COMMENT = discord.Color.blurple()
COLOR_OPENED = discord.Color.green()
COLOR_CLOSED = discord.Color.dark_grey()
COLOR_MERGED = discord.Color.purple()
COLOR_RELEASE = discord.Color.gold()
COLOR_PUSH = discord.Color.teal()
# ブランチ push の ref 接頭辞（タグ push と区別する）
BRANCH_REF_PREFIX = "refs/heads/"
# コミット一覧で表示するハッシュの桁数と、1行あたりのメッセージ上限
SHORT_SHA_LEN = 7
COMMIT_LINE_CHARS = 100
# issues イベントで通知するアクションと表示名
ISSUE_ACTIONS = {
    "opened": ("Issue 作成", COLOR_OPENED),
    "closed": ("Issue クローズ", COLOR_CLOSED),
    "reopened": ("Issue 再オープン", COLOR_OPENED),
}
# pull_request イベントで通知するアクションと表示名
PR_ACTIONS = {
    "opened": ("PR 作成", COLOR_OPENED),
    "closed": ("PR クローズ", COLOR_CLOSED),
    "reopened": ("PR 再オープン", COLOR_OPENED),
}
# 通知の分類（送信先チャンネルの振り分けに使う）
CATEGORY_ISSUE = "issue"
CATEGORY_RELEASE = "release"
CATEGORY_COMMIT = "commit"


@dataclass
class Notification:
    """送信する埋め込みと、振り分けに必要な情報。"""

    embed: discord.Embed
    category: str
    repo: str
    number: int | None = None


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """X-Hub-Signature-256 ヘッダーを検証する。"""
    # シークレット未設定・署名なしは常に不正扱い
    if not secret or not signature:
        return False
    # 本文の HMAC-SHA256 を計算して定数時間で比較する
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def _title(repo: str, kind: str, item: dict[str, Any]) -> str:
    """「[owner/repo] Issue #12: タイトル」形式の見出しを作る。"""
    return f"[{repo}] {kind} #{item.get('number')}: {item.get('title', '')}"


class GitHubCog(commands.Cog):
    """GitHub Webhook を受け取り、Discord へ通知を転送する。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # aiohttp サーバーのランナー（停止時に使う）
        self._runner: web.AppRunner | None = None
        # 処理済み Delivery ID（再送による重複通知の防止）
        self._seen: deque[str] = deque(maxlen=SEEN_DELIVERY_LIMIT)
        # 実行中の通知タスク（GC で消えないよう参照を保持）
        self._tasks: set[asyncio.Task[None]] = set()

    async def cog_load(self) -> None:
        """Cog ロード時に Webhook サーバーを起動する。"""
        await self.start_server()

    async def cog_unload(self) -> None:
        """Cog アンロード時に Webhook サーバーを止める。"""
        await self.stop_server()

    # ------------------------------------------------------------
    # サーバーの起動・停止
    # ------------------------------------------------------------
    async def start_server(self) -> None:
        """設定に従って Webhook 受信サーバーを起動する。"""
        cfg = self.bot.config.github
        # 機能 OFF なら起動しない
        if not cfg.enabled:
            return
        # 署名検証なしで外部公開しないよう、シークレット必須にする
        if not cfg.secret:
            log.error("github.secret が未設定のため Webhook サーバーを起動しません")
            return
        app = web.Application()
        app.router.add_post(cfg.path, self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, cfg.host, cfg.port).start()
        except OSError as exc:
            # ポート使用中などで起動できなくてもボット本体は動かし続ける
            log.error("Webhook サーバーを起動できません (%s:%s): %s",
                      cfg.host, cfg.port, exc)
            await runner.cleanup()
            return
        self._runner = runner
        log.info("GitHub Webhook を待ち受け中: http://%s:%s%s",
                 cfg.host, cfg.port, cfg.path)

    async def stop_server(self) -> None:
        """Webhook 受信サーバーを停止する。"""
        # 起動していなければ何もしない
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None

    async def restart_server(self) -> None:
        """設定再読込後にサーバーを作り直す。"""
        await self.stop_server()
        await self.start_server()

    @property
    def is_running(self) -> bool:
        """サーバーが稼働中か。"""
        return self._runner is not None

    # ------------------------------------------------------------
    # リクエスト処理
    # ------------------------------------------------------------
    async def _handle(self, request: web.Request) -> web.Response:
        """Webhook リクエストを検証し、通知処理をバックグラウンドで始める。"""
        cfg = self.bot.config.github
        body = await request.read()
        # 署名が一致しないリクエストは拒否する
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(cfg.secret, body, signature):
            log.warning("GitHub Webhook の署名検証に失敗しました (%s)",
                        request.remote)
            return web.Response(status=401, text="invalid signature")
        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        # 登録時の疎通確認には即応答する
        if event == "ping":
            return web.Response(text="pong")
        # 同じ配信の再送は無視する
        if delivery and delivery in self._seen:
            log.info("GitHub: 重複配信のため無視しました (delivery=%s)", delivery)
            return web.Response(status=200, text="duplicate")
        self._seen.append(delivery)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")
        # 受信内容をログに残す（届いているかの切り分け用）
        log.info(
            "GitHub: 受信 event=%s action=%s repo=%s delivery=%s",
            event,
            payload.get("action"),
            (payload.get("repository") or {}).get("full_name"),
            delivery,
        )
        # GitHub のタイムアウト（10秒）を避けるため、送信は後回しにして即応答する
        task = asyncio.create_task(self._dispatch(event, payload))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return web.Response(status=202, text="accepted")

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """通知タスクの後始末と、想定外の例外の記録。"""
        self._tasks.discard(task)
        # キャンセルされたタスクは例外を取り出せない
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("GitHub: 通知処理で例外が発生しました", exc_info=exc)

    async def _dispatch(self, event: str, payload: dict[str, Any]) -> None:
        """イベントを埋め込みに変換して送信する。"""
        cfg = self.bot.config.github
        events = cfg.events or DEFAULT_EVENTS
        # 通知対象外のイベントは無視する
        if not events.get(event, False):
            log.info("GitHub: events で無効のため無視しました (%s)", event)
            return
        # ボット（dependabot 等）の投稿を除外する
        # リリースは GitHub Actions 等のボットが公開することが多いので対象外
        sender = payload.get("sender") or {}
        is_bot = sender.get("type") == "Bot"
        if cfg.ignore_bots and is_bot and event != "release":
            log.info("GitHub: ボットの操作のため無視しました (%s)",
                     sender.get("login"))
            return
        notification = self._build(event, payload)
        # 通知対象外のアクション（編集・削除など）は捨てる
        if notification is None:
            log.info("GitHub: 対象外のアクションのため無視しました (%s/%s)",
                     event, payload.get("action"))
            return
        await self._send(notification)
        # VRCast リリース公開時はサポート RAG を差分更新する
        if event == "release" and payload.get("action") == "published":
            await self._maybe_sync_rag(payload)

    # ------------------------------------------------------------
    # 埋め込み生成
    # ------------------------------------------------------------
    @staticmethod
    def _clean_body(body: str | None, limit: int) -> str:
        """本文からテンプレートの HTML コメントを除き、長さを制限する。"""
        text = HTML_COMMENT_RE.sub("", body or "").strip()
        return truncate(text, limit) or "（本文なし）"

    def _build(
        self, event: str, payload: dict[str, Any]
    ) -> Notification | None:
        """イベント種別に応じた通知を作る（対象外なら None）。"""
        action = payload.get("action")
        repo = str((payload.get("repository") or {}).get("full_name", "?"))
        # Issue / PR への通常コメント
        if event == "issue_comment" and action == "created":
            issue = payload.get("issue") or {}
            comment = payload.get("comment") or {}
            kind = "PR" if "pull_request" in issue else "Issue"
            embed = self._embed(
                title=_title(repo, kind, issue),
                url=comment.get("html_url"),
                user=comment.get("user") or {},
                body=comment.get("body"),
                footer=f"{kind} に新しいコメント",
                color=COLOR_COMMENT,
            )
            return Notification(embed, CATEGORY_ISSUE, repo, issue.get("number"))
        # PR の差分行へのレビューコメント
        if event == "pull_request_review_comment" and action == "created":
            pr = payload.get("pull_request") or {}
            comment = payload.get("comment") or {}
            embed = self._embed(
                title=_title(repo, "PR", pr),
                url=comment.get("html_url"),
                user=comment.get("user") or {},
                body=comment.get("body"),
                footer="PR にレビューコメント",
                color=COLOR_COMMENT,
            )
            embed.add_field(name="ファイル", value=f"`{comment.get('path', '-')}`")
            return Notification(embed, CATEGORY_ISSUE, repo, pr.get("number"))
        # Issue の作成・クローズ・再オープン
        if event == "issues" and action in ISSUE_ACTIONS:
            issue = payload.get("issue") or {}
            label, color = ISSUE_ACTIONS[action]
            embed = self._embed(
                title=_title(repo, "Issue", issue),
                url=issue.get("html_url"),
                user=payload.get("sender") or {},
                body=issue.get("body") if action == "opened" else "",
                footer=label,
                color=color,
            )
            return Notification(embed, CATEGORY_ISSUE, repo, issue.get("number"))
        # PR の作成・クローズ（マージ）・再オープン
        if event == "pull_request" and action in PR_ACTIONS:
            pr = payload.get("pull_request") or {}
            label, color = PR_ACTIONS[action]
            # クローズのうちマージされたものは表示を変える
            if action == "closed" and pr.get("merged"):
                label, color = "PR マージ", COLOR_MERGED
            embed = self._embed(
                title=_title(repo, "PR", pr),
                url=pr.get("html_url"),
                user=payload.get("sender") or {},
                body=pr.get("body") if action == "opened" else "",
                footer=label,
                color=color,
            )
            return Notification(embed, CATEGORY_ISSUE, repo, pr.get("number"))
        # リリースの公開（下書きは published にならない）
        if event == "release" and action == "published":
            release = payload.get("release") or {}
            tag = release.get("tag_name", "")
            name = release.get("name") or tag
            label = "プレリリース公開" if release.get("prerelease") else "リリース公開"
            embed = self._embed(
                title=f"[{repo}] {name}",
                url=release.get("html_url"),
                user=release.get("author") or payload.get("sender") or {},
                body=release.get("body"),
                footer=label,
                color=COLOR_RELEASE,
                limit=self.bot.config.github.max_release_body_chars,
            )
            embed.add_field(name="タグ", value=f"`{tag}`")
            return Notification(embed, CATEGORY_RELEASE, repo)
        # ブランチへのコミット push
        if event == "push":
            return self._build_push(repo, payload)
        return None

    def _build_push(
        self, repo: str, payload: dict[str, Any]
    ) -> Notification | None:
        """push イベントからコミット一覧の通知を作る（対象外なら None）。"""
        cfg = self.bot.config.github
        ref = str(payload.get("ref", ""))
        # タグの push はリリース通知と重なるので対象外
        if not ref.startswith(BRANCH_REF_PREFIX):
            return None
        branch = ref.removeprefix(BRANCH_REF_PREFIX)
        # ブランチ指定があれば、それ以外のブランチは無視する
        if cfg.commit_branches and branch not in cfg.commit_branches:
            return None
        commits = payload.get("commits") or []
        # ブランチ削除やコミットを伴わない push は通知しない
        if payload.get("deleted") or not commits:
            return None
        # 表示件数を制限し、1件1行の一覧にする（最低1件は表示）
        shown = commits[: max(cfg.max_commits, 1)]
        lines = [self._commit_line(commit) for commit in shown]
        # 表示しきれなかった件数を末尾に添える
        rest = len(commits) - len(shown)
        if rest > 0:
            lines.append(f"…ほか {rest} 件")
        label = "強制 push" if payload.get("forced") else "コミット push"
        embed = self._embed(
            title=f"[{repo}:{branch}] {len(commits)} 件のコミット",
            url=payload.get("compare"),
            user=payload.get("sender") or {},
            body=None,
            footer=label,
            color=COLOR_PUSH,
        )
        # 一覧はテンプレート除去・本文上限の対象外なので直接設定する
        embed.description = truncate("\n".join(lines), 4000)
        return Notification(embed, CATEGORY_COMMIT, repo)

    @staticmethod
    def _commit_line(commit: dict[str, Any]) -> str:
        """コミット1件を「[`abc1234`](URL) 件名 - 作者」形式にする。"""
        sha = str(commit.get("id", ""))[:SHORT_SHA_LEN]
        # 件名（メッセージの1行目）だけを使う
        message = str(commit.get("message", "")).splitlines()
        subject = truncate(message[0] if message else "", COMMIT_LINE_CHARS)
        author = commit.get("author") or {}
        # GitHub ユーザー名が無ければ Git の作者名を使う
        name = author.get("username") or author.get("name") or "unknown"
        return f"[`{sha}`]({commit.get('url', '')}) {subject} - {name}"

    def _embed(
        self,
        *,
        title: str,
        url: str | None,
        user: dict[str, Any],
        body: str | None,
        footer: str,
        color: discord.Color,
        limit: int | None = None,
    ) -> discord.Embed:
        """共通レイアウトの埋め込みを組み立てる。"""
        # 本文の上限は指定が無ければコメント用の値を使う
        max_chars = limit or self.bot.config.github.max_body_chars
        # 本文が空（クローズ通知など）なら説明文を付けない
        description = self._clean_body(body, max_chars) if body else ""
        embed = make_embed(title, description, color)
        embed.url = url
        # コメント投稿者をアイコン付きで表示する
        embed.set_author(
            name=str(user.get("login", "unknown")),
            url=user.get("html_url"),
            icon_url=user.get("avatar_url"),
        )
        embed.set_footer(text=footer)
        return embed

    def _resolve_channel(self, notification: Notification) -> int:
        """通知の分類・リポジトリ・番号から送信先チャンネルを決める。"""
        cfg = self.bot.config.github
        # リリースは専用チャンネル → 既定チャンネルの順
        if notification.category == CATEGORY_RELEASE:
            return cfg.release_channel_id or cfg.default_channel_id
        # コミットは専用チャンネル → 既定チャンネルの順
        if notification.category == CATEGORY_COMMIT:
            return cfg.commit_channel_id or cfg.default_channel_id
        # Issue / PR は番号別ルートを上から順に探す
        repo = notification.repo.lower()
        for route in cfg.issue_routes:
            # repo 指定があるルートは、そのリポジトリにだけ適用する
            if route.repo and route.repo != repo:
                continue
            if notification.number in route.numbers:
                return route.channel_id
        # どのルートにも当たらなければ Issue 用 → 既定チャンネルの順
        return cfg.issue_channel_id or cfg.default_channel_id

    async def _maybe_sync_rag(self, payload: dict[str, Any]) -> None:
        """サポート対象リポジトリのリリースなら RAG 差分同期をキックする。"""
        support = self.bot.get_cog("SupportCog")
        if support is None:
            return
        repo = str((payload.get("repository") or {}).get("full_name", ""))
        release = payload.get("release") or {}
        tag = str(release.get("tag_name") or "")
        body = str(release.get("body") or "")
        if not tag:
            return
        try:
            await support.on_release_published(repo, tag, body)  # type: ignore[attr-defined]
        except Exception:
            log.exception("GitHub: RAG sync kick failed")

    async def _send(self, notification: Notification) -> None:
        """振り分け先のチャンネルへ送信する。"""
        repo = notification.repo
        embed = notification.embed
        channel_id = self._resolve_channel(notification)
        # 送信先が決まらなければ捨てる
        if not channel_id:
            log.warning("GitHub: 送信先チャンネル未設定のため通知を捨てました "
                        "(%s #%s)", repo, notification.number)
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            # フォーラム・カテゴリ等は直接送信できない
            if not isinstance(channel, discord.abc.Messageable):
                log.warning("GitHub: チャンネル %s は送信できない種類です (%s)",
                            channel_id, type(channel).__name__)
                return
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            log.info("GitHub: 通知を送信しました (%s #%s → %s)",
                     repo, notification.number, channel_id)
        except discord.HTTPException as exc:
            log.warning("GitHub: 通知の送信に失敗しました (%s → %s): %s",
                        repo, channel_id, exc)


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(GitHubCog(bot))
