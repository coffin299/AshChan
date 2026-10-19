"""dev通知チャンネル・公開ログ・緊急DM への送信を一元化する。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ash.utils import truncate

if TYPE_CHECKING:
    from ash.bot import AshBot

log = logging.getLogger(__name__)

# 通知の重要度ごとの埋め込み色
COLOR_INFO = discord.Color.blue()
COLOR_SUCCESS = discord.Color.green()
COLOR_WARNING = discord.Color.orange()
COLOR_DANGER = discord.Color.red()


def make_embed(
    title: str,
    description: str = "",
    color: discord.Color = COLOR_INFO,
    fields: dict[str, str] | None = None,
) -> discord.Embed:
    """通知用の埋め込みを組み立てる。"""
    embed = discord.Embed(
        title=truncate(title, 256),
        description=truncate(description, 4000),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    for name, value in (fields or {}).items():
        embed.add_field(
            name=truncate(name, 256),
            value=truncate(value or "-", 1024),
            inline=False,
        )
    return embed


class Notifier:
    """dev通知・公開ログ・緊急DMを送るヘルパー。"""

    def __init__(self, bot: AshBot) -> None:
        self._bot = bot

    async def _send_to_channel(
        self, channel_id: int, embed: discord.Embed, *, label: str
    ) -> bool:
        """指定チャンネルへ埋め込みを送る。成功なら True。"""
        if not channel_id:
            return False
        try:
            channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
                return True
        except discord.HTTPException as exc:
            log.warning("%s通知の送信に失敗しました: %s", label, exc)
        return False

    async def notify_dev(self, embed: discord.Embed) -> None:
        """dev通知チャンネルへ送る（未設定ならログのみ）。"""
        log.info("[dev] %s | %s", embed.title, embed.description)
        await self._send_to_channel(
            self._bot.config.notify.dev_channel_id, embed, label="dev"
        )

    async def notify_public(self, embed: discord.Embed) -> None:
        """公開してよい稼働ログを送る。

        public_channel_id があればそこへ。無ければ / 失敗時は dev へフォールバック。
        """
        log.info("[public] %s | %s", embed.title, embed.description)
        public_id = int(self._bot.config.notify.public_channel_id or 0)
        if public_id and await self._send_to_channel(
            public_id, embed, label="public"
        ):
            return
        await self._send_to_channel(
            self._bot.config.notify.dev_channel_id, embed, label="public→dev"
        )

    async def notify_emergency(self, embed: discord.Embed) -> None:
        """緊急通知: 常に非公開の dev 通知に加え、指定ユーザーへ DM する。"""
        await self.notify_dev(embed)
        for user_id in self._bot.config.notify.emergency_dm_user_ids:
            try:
                user = self._bot.get_user(user_id)
                if user is None:
                    user = await self._bot.fetch_user(user_id)
                await user.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("緊急DMの送信に失敗しました (%s): %s", user_id, exc)
