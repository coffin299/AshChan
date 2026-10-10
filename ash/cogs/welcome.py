"""ようこそメッセージ: 新規参加者へアッシュの口調で 1〜3 行の挨拶を送る。"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.llm.client import LLMError

log = logging.getLogger(__name__)

# 1通あたりの本文の最大文字数（長すぎる生成を切り詰める）
MAX_WELCOME_CHARS = 400
# LLM 呼び出し回数を数える時間窓（秒）
LLM_RATE_WINDOW = 60.0
# fallback_messages が空のときに使う既定の定型文（{name} / {server} を置換）
DEFAULT_FALLBACKS = (
    "{name}、いらっしゃい！ ゆっくりしてってね。",
    "お、新しい人だ。{name}、{server} へようこそ！",
    "{name}、来てくれてありがと。気楽にいこ！",
)
# キャラ設定の後ろに付け足す、挨拶生成用の指示
WELCOME_NOTES = """

## ようこそメッセージの書き方（システム補足）
- サーバーに新しく参加したメンバーへの歓迎メッセージを書く。
- {max_lines}行以内、全体で120文字程度まで。短く自然に。
- 相手の名前は呼んでよいが、@メンションや「アッシュ:」などの名前の接頭辞は書かない。
- 毎回同じ定型句にならないよう、言い回しを変える。
"""
# LLM が付けがちな「アッシュ:」等の接頭辞
SPEAKER_PREFIX_RE = re.compile(r"^(?:アッシュ|Ash)\s*[:：]\s*", re.IGNORECASE)


class WelcomeCog(commands.Cog):
    """新規参加者への歓迎メッセージ。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # 直近の LLM 呼び出し時刻（大量参加時の呼びすぎ防止）
        self._llm_calls: deque[float] = deque()

    # ------------------------------------------------------------
    # 参加イベント
    # ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """参加時に挨拶する（メンバーシップ審査待ちなら保留）。"""
        cfg = self.bot.config.welcome
        # 機能 OFF・ボットの参加は対象外
        if not cfg.enabled or member.bot:
            return
        # 審査（ルール同意）待ちなら、通過時に挨拶する
        if cfg.wait_for_screening and member.pending:
            return
        await self._welcome(member)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """メンバーシップ審査を通過したタイミングで挨拶する。"""
        cfg = self.bot.config.welcome
        # 機能 OFF・審査待ちを使わない設定・ボットは対象外
        if not cfg.enabled or not cfg.wait_for_screening or after.bot:
            return
        # pending が True → False に変わったときだけ挨拶する
        if before.pending and not after.pending:
            await self._welcome(after)

    # ------------------------------------------------------------
    # 送信
    # ------------------------------------------------------------
    def _target_channel(self, guild: discord.Guild) -> discord.abc.Messageable | None:
        """着地チャンネルを返す（未設定ならサーバーのシステムチャンネル）。"""
        channel_id = self.bot.config.welcome.channel_id
        channel = guild.get_channel(channel_id) if channel_id else guild.system_channel
        # 送信できない種類・見つからない場合は None
        if isinstance(channel, discord.abc.Messageable):
            return channel
        return None

    async def _welcome(self, member: discord.Member) -> None:
        """挨拶文を作って着地チャンネルへ送る。"""
        channel = self._target_channel(member.guild)
        if channel is None:
            log.warning("ようこそメッセージの送信先が見つかりません (%s)", member.guild)
            return
        # 生成中は「入力中...」を表示する
        async with channel.typing():
            text = await self._compose(member)
        try:
            # 全員に見える通常投稿。メンション通知は本人だけに飛ばす
            await channel.send(
                f"{member.mention} {text}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[member]
                ),
            )
        except discord.HTTPException as exc:
            log.warning("ようこそメッセージの送信に失敗しました: %s", exc)

    # ------------------------------------------------------------
    # 文面の生成
    # ------------------------------------------------------------
    def _llm_allowed(self) -> bool:
        """時間窓内の LLM 呼び出しが上限未満か判定し、記録する。"""
        limit = self.bot.config.welcome.max_llm_per_minute
        now = time.monotonic()
        # 時間窓より古い記録を捨てる
        while self._llm_calls and now - self._llm_calls[0] > LLM_RATE_WINDOW:
            self._llm_calls.popleft()
        # 上限に達していれば定型文に切り替える（0 なら常に定型文）
        if len(self._llm_calls) >= limit:
            return False
        self._llm_calls.append(now)
        return True

    async def _compose(self, member: discord.Member) -> str:
        """LLM で挨拶文を作る（失敗・上限超過時は定型文）。"""
        # 大量参加時は LLM を呼ばず定型文にする
        if not self._llm_allowed():
            return self._fallback(member)
        cfg = self.bot.config.welcome
        system = self.bot.system_prompt + WELCOME_NOTES.format(
            max_lines=cfg.max_lines
        )
        # 参加者とサーバーの情報を渡す
        lines = [
            f"新しく参加したメンバー: {member.display_name}",
            f"サーバー名: {member.guild.name}",
            f"メンバー数: {member.guild.member_count} 人目",
        ]
        if cfg.extra_instructions:
            lines.append(f"補足: {cfg.extra_instructions}")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)},
        ]
        try:
            text = await self.bot.llm.complete(messages)
        except LLMError:
            # NIM エラー時は定型文で挨拶する
            return self._fallback(member)
        return self._sanitize(text, cfg.max_lines) or self._fallback(member)

    @staticmethod
    def _sanitize(text: str, max_lines: int) -> str:
        """接頭辞・空行を除き、行数と文字数を制限する。"""
        # 「アッシュ:」などの接頭辞を外す
        text = SPEAKER_PREFIX_RE.sub("", text.strip())
        # 空行を除いて指定行数までに絞る
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        result = "\n".join(lines[: max(max_lines, 1)])
        # 長すぎる場合は切り詰める
        if len(result) > MAX_WELCOME_CHARS:
            result = result[: MAX_WELCOME_CHARS - 1] + "…"
        return result

    def _fallback(self, member: discord.Member) -> str:
        """定型文をランダムに選び、名前とサーバー名を埋め込む。"""
        candidates = self.bot.config.welcome.fallback_messages or list(
            DEFAULT_FALLBACKS
        )
        template = random.choice(candidates)
        return template.replace("{name}", member.display_name).replace(
            "{server}", member.guild.name
        )


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(WelcomeCog(bot))
