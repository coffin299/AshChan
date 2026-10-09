"""アッシュの会話機能: メンション/リプライに NIM でストリーミング応答する。"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.llm.client import LLMError, StreamEvent
from ash.llm.search import WEB_SEARCH_TOOL, SearchError, format_results, web_search
from ash.llm.stream_view import StreamView

log = logging.getLogger(__name__)

# 日本時間（システムプロンプトに現在時刻を入れるため）
JST = timezone(timedelta(hours=9))
# 参照リンクとして末尾に付ける最大件数
MAX_SOURCES = 5
# fallback_replies が空のときに使う既定の定型文
DEFAULT_FALLBACKS = (
    "ごめん、今ちょっと手が離せないや。あとでまた声かけて！",
    "んー、今忙しいからあとでね！",
    "ちょっと頭回ってないかも……少し待ってからもう一回言って？",
    "今は休憩中ー。あとでちゃんと聞くから！",
)
# Discord 運用のためにキャラ設定の後ろへ付け足す指示
OPERATION_NOTES = """

## Discordでの運用ルール（システム補足）
- あなたは Discord サーバーのチャットに参加している。
- ユーザーの発言は「名前: 内容」の形式で渡される。返答の先頭に自分の名前を付けないこと。
- 返答は Discord の Markdown で書ける。長くても1500文字程度に収める。
- 最新情報・ニュース・知らない固有名詞など、調べたほうが正確なときは web_search ツールを使う。
- 検索結果を使ったときは、内容を自分の言葉で要約して伝える（URLの羅列はしない）。
- 現在日時: {now}
"""


class ChatCog(commands.Cog):
    """メンション・リプライへの LLM 応答。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # ユーザーごとの最終応答時刻（クールダウン用）
        self._last_reply: dict[int, float] = {}

    # ------------------------------------------------------------
    # トリガー判定
    # ------------------------------------------------------------
    def _is_triggered(self, message: discord.Message) -> bool:
        """ボットへのメンションまたはボット発言へのリプライか判定する。"""
        me = self.bot.user
        # ログイン前は何もしない
        if me is None:
            return False
        # 直接メンションされていれば対象
        if me in message.mentions:
            return True
        # リプライ先がボット自身の発言なら対象
        ref = message.reference
        if ref is None:
            return False
        target = ref.resolved or ref.cached_message
        return isinstance(target, discord.Message) and target.author.id == me.id

    def _cooldown_active(self, user_id: int) -> bool:
        """ユーザーがクールダウン中か判定し、そうでなければ時刻を記録する。"""
        cooldown = self.bot.config.llm.user_cooldown_sec
        now = time.monotonic()
        # 前回応答から cooldown 秒経っていなければクールダウン中
        if cooldown > 0 and now - self._last_reply.get(user_id, 0.0) < cooldown:
            return True
        # 応答するので時刻を更新する
        self._last_reply[user_id] = now
        return False

    # ------------------------------------------------------------
    # プロンプト組み立て
    # ------------------------------------------------------------
    def _clean_content(self, message: discord.Message) -> str:
        """ボットへのメンションを除去し、他のメンションを名前に置換する。"""
        me = self.bot.user
        text = message.content
        # ボット自身へのメンションを取り除く
        if me is not None:
            text = re.sub(rf"<@!?{me.id}>", "", text)
        # 他ユーザーのメンションは表示名に置き換える
        for user in message.mentions:
            text = re.sub(rf"<@!?{user.id}>", f"@{user.display_name}", text)
        # ロールメンションもロール名に置き換える
        for role in message.role_mentions:
            text = text.replace(f"<@&{role.id}>", f"@{role.name}")
        return text.strip()

    def _format_user_turn(self, message: discord.Message) -> str:
        """ユーザー発言を「名前: 内容」形式にする（返信先があれば添える）。"""
        body = self._clean_content(message) or "（名前を呼んだだけ）"
        # 添付ファイルがあればその旨だけ伝える
        if message.attachments:
            body += f"\n（添付ファイル {len(message.attachments)} 件）"
        line = f"{message.author.display_name}: {body}"
        # ボット以外の発言への返信なら、返信先を引用として添える
        ref = message.reference
        target = (ref.resolved or ref.cached_message) if ref else None
        me = self.bot.user
        if (
            isinstance(target, discord.Message)
            and me is not None
            and target.author.id != me.id
        ):
            quoted = target.content[:300]
            line = f"（{target.author.display_name} の発言「{quoted}」への返信）\n{line}"
        return line

    def _system_prompt(self) -> str:
        """キャラ設定に運用ルールと現在時刻を付け足す。"""
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M (JST)")
        return self.bot.system_prompt + OPERATION_NOTES.format(now=now)

    def _fallback(self) -> str:
        """NIM エラー時の定型文をランダムに選ぶ。"""
        candidates = self.bot.config.llm.fallback_replies or list(DEFAULT_FALLBACKS)
        return random.choice(candidates)

    # ------------------------------------------------------------
    # 応答生成
    # ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """メンション/リプライを受けて応答する。"""
        # サーバー外・ボット・Webhook の発言は無視する
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        # ハニーポットチャンネルでは会話しない
        if message.channel.id in self.bot.config.honeypot.channel_ids:
            return
        # トリガー条件を満たさなければ無視する
        if not self._is_triggered(message):
            return
        # クールダウン中はリアクションだけ付けて無視する
        if self._cooldown_active(message.author.id):
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass
            return
        await self._respond(message)

    async def _respond(self, message: discord.Message) -> None:
        """会話履歴を踏まえて LLM 応答をストリーミング表示する。"""
        cfg = self.bot.config
        channel_id = message.channel.id
        user_turn = self._format_user_turn(message)
        # 直近の履歴（1往復 = 2件）を取得する
        history = await self.bot.db.get_history(channel_id, cfg.llm.history_turns * 2)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            *history,
            {"role": "user", "content": user_turn},
        ]
        view = StreamView(message, cfg.llm.stream_edit_interval_sec)
        sources: list[str] = []
        failed = False

        # 生成中は「入力中...」を表示する
        async with message.channel.typing():
            try:
                await self._generate(messages, view, sources)
            except LLMError:
                # エラーは dev 通知せず、定型文で返す
                failed = True

        # 失敗時・空応答時は定型文で締める
        if failed or not view.has_output:
            suffix = ("\n\n" if view.has_output else "") + self._fallback()
            await view.finalize(suffix)
            return
        # 参照リンクを小さく付記して確定する
        await view.finalize(self._sources_suffix(sources))
        # 成功した会話だけを履歴に保存する
        await self.bot.db.add_history(channel_id, "user", user_turn)
        await self.bot.db.add_history(channel_id, "assistant", view.full_text.strip())
        await self.bot.db.trim_history(channel_id, cfg.llm.history_turns * 2)

    async def _generate(
        self,
        messages: list[dict[str, Any]],
        view: StreamView,
        sources: list[str],
    ) -> None:
        """ストリーミング生成と Web 検索のループ。"""
        search_cfg = self.bot.config.search
        calls_used = 0
        while True:
            # 検索が有効かつ上限未満のときだけツールを提示する
            can_search = (
                search_cfg.enabled and calls_used < search_cfg.max_calls_per_reply
            )
            tools = [WEB_SEARCH_TOOL] if can_search else None
            round_text = ""
            tool_calls = []
            # 1ラウンド分のストリームを処理する
            event: StreamEvent
            async for event in self.bot.llm.stream_chat(messages, tools):
                if event.kind == "text":
                    round_text += event.text
                    await view.append(event.text)
                elif event.kind == "tool_calls":
                    tool_calls = event.tool_calls
            # ツール呼び出しが無ければ回答完了
            if not tool_calls:
                return
            # ツール呼び出しを含むアシスタント発言を文脈に積む
            messages.append(
                {
                    "role": "assistant",
                    "content": round_text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in tool_calls
                    ],
                }
            )
            # 各ツール呼び出しを実行し、結果を tool メッセージとして返す
            for call in tool_calls:
                calls_used += 1
                result = await self._run_tool(call.name, call.arguments, view, sources)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
            # 検索中表示を消して回答の続きを流す
            await view.set_status(None)
            # 本文の続きは改行を挟んで書き始める
            if view.has_output:
                await view.append("\n")

    async def _run_tool(
        self,
        name: str,
        arguments: str,
        view: StreamView,
        sources: list[str],
    ) -> str:
        """ツールを実行し、LLM に返す文字列を作る。"""
        # 未知のツールはエラーとして返す
        if name != "web_search":
            return f"未知のツール: {name}"
        # 引数 JSON からクエリを取り出す
        try:
            query = str(json.loads(arguments or "{}").get("query", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            query = ""
        if not query:
            return "検索クエリが空でした。"
        search_cfg = self.bot.config.search
        # それまでの回答の末尾に検索中表示を出す
        await view.set_status(search_cfg.indicator.replace("{query}", query))
        try:
            results = await web_search(
                query, search_cfg.region, search_cfg.max_results
            )
        except SearchError:
            # 検索失敗は LLM に伝えて手持ちの知識で答えさせる
            return "検索に失敗しました。手持ちの知識で答えてください。"
        # 参照リンク候補を重複なしで集める
        for r in results:
            if r.url not in sources:
                sources.append(r.url)
        return format_results(results)

    def _sources_suffix(self, sources: list[str]) -> str:
        """参照リンクの小さな注記を作る（埋め込みは抑止）。"""
        # 表示設定が OFF または検索していなければ何も付けない
        if not self.bot.config.search.show_sources or not sources:
            return ""
        links = " ".join(f"<{url}>" for url in sources[:MAX_SOURCES])
        return f"\n-# 参考: {links}"


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(ChatCog(bot))
