"""dev通知チャンネル・緊急DM への送信を一元化する。"""

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
    # タイトル・本文・色・時刻を設定する
    embed = discord.Embed(
        title=truncate(title, 256),
        description=truncate(description, 4000),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    # フィールドを順に追加する（値が空なら "-" にする）
    for name, value in (fields or {}).items():
        embed.add_field(
            name=truncate(name, 256),
            value=truncate(value or "-", 1024),
            inline=False,
        )
    return embed


class Notifier:
    """dev通知と緊急DMを送るヘルパー。"""

    def __init__(self, bot: AshBot) -> None:
        # 設定と Discord API へのアクセスに Bot を使う
        self._bot = bot

    async def notify_dev(self, embed: discord.Embed) -> None:
        """dev通知チャンネルへ送る（未設定ならログのみ）。"""
        # まずコンソールにも残す
        log.info("[dev] %s | %s", embed.title, embed.description)
        # 送信先チャンネル ID を取得する
        channel_id = self._bot.config.notify.dev_channel_id
        # 未設定ならログのみで終了する
        if not channel_id:
            return
        try:
            # キャッシュに無ければ API から取得する
            channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(channel_id)
            # 送信可能なチャンネルのときだけ送る
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
        except discord.HTTPException as exc:
            # 通知失敗でボットが止まらないようにログに留める
            log.warning("dev通知の送信に失敗しました: %s", exc)

    async def notify_emergency(self, embed: discord.Embed) -> None:
        """緊急通知: dev通知に加えて指定ユーザーへDMする。"""
        # dev通知チャンネルにも同じ内容を残す
        await self.notify_dev(embed)
        # 設定された全ユーザーへ DM する
        for user_id in self._bot.config.notify.emergency_dm_user_ids:
            try:
                # キャッシュに無ければ API から取得する
                user = self._bot.get_user(user_id)
                if user is None:
                    user = await self._bot.fetch_user(user_id)
                await user.send(embed=embed)
            except discord.HTTPException as exc:
                # DM 拒否等は警告ログのみ
                log.warning("緊急DMの送信に失敗しました (%s): %s", user_id, exc)
