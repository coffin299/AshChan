"""ハニーポット: 罠チャンネル投稿者を段階的に処罰する（キック+全投稿削除 → BAN）。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from ash.bot import AshBot
from ash.notifier import COLOR_DANGER, COLOR_WARNING, make_embed
from ash.utils import has_any_role

log = logging.getLogger(__name__)

# BAN 時に削除するメッセージの期間（Discord の上限 7 日）
BAN_DELETE_SECONDS = 7 * 24 * 60 * 60
# 一括削除が使える期間（Discord の制約で 14 日未満）
BULK_DELETE_MAX_AGE = 14 * 24 * 60 * 60 - 60
# 一括削除1回あたりの最大件数
BULK_DELETE_CHUNK = 100


def honeypot_reason(count: int) -> str:
    """Kick/BAN 理由の文字列を作る。"""
    return f"ハニーポット検知({count}回目)"


class HoneypotCog(commands.Cog):
    """罠チャンネルの監視・処罰と、全投稿削除用の投稿インデックス。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # 同一ユーザーの処罰を多重実行しないためのロック
        self._locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        """Cog ロード時に古いインデックスの定期掃除を開始する。"""
        self.prune_index.start()

    async def cog_unload(self) -> None:
        """Cog アンロード時に定期掃除を止める。"""
        self.prune_index.cancel()

    # ------------------------------------------------------------
    # 投稿インデックス
    # ------------------------------------------------------------
    @tasks.loop(hours=6)
    async def prune_index(self) -> None:
        """保持期間を過ぎた投稿インデックスを削除する。"""
        days = self.bot.config.honeypot.purge_scan_days
        removed = await self.bot.db.prune_messages(time.time() - days * 86400)
        if removed:
            log.info("投稿インデックスを %d 件掃除しました", removed)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """削除されたメッセージをインデックスからも消す。"""
        if not self.bot.is_guild_allowed(payload.guild_id):
            return
        await self.bot.db.forget_message(payload.message_id)

    # ------------------------------------------------------------
    # 罠チャンネル監視
    # ------------------------------------------------------------
    def _is_exempt(self, member: discord.abc.User, guild: discord.Guild) -> bool:
        """ハニーポットの対象外か判定する。"""
        # ボット・オーナーは常に対象外
        if member.bot or member.id == guild.owner_id:
            return True
        # 回避ロールを持っていれば対象外
        return has_any_role(member, self.bot.config.honeypot.exempt_role_ids)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """全投稿をインデックスし、罠チャンネルへの投稿を処罰する。"""
        guild = message.guild
        # サーバー外・ボット・Webhook は対象外
        if guild is None or message.author.bot or message.webhook_id:
            return
        if not self.bot.is_guild_allowed(guild.id):
            return
        cfg = self.bot.config.honeypot
        # 全投稿削除のために投稿をインデックスする
        if cfg.enabled:
            await self.bot.db.index_message(
                message.id,
                guild.id,
                message.channel.id,
                message.author.id,
                message.created_at.timestamp(),
            )
        # 罠チャンネル以外・機能 OFF なら終了
        if not cfg.enabled or message.channel.id not in cfg.channel_ids:
            return
        # 回避対象ならカウントもしない
        if self._is_exempt(message.author, guild):
            return
        # 罠への投稿は即削除する
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        lock = self._locks[(guild.id, message.author.id)]
        # 処罰中の連投はカウントせず削除のみで済ませる
        if lock.locked():
            return
        async with lock:
            await self._punish(guild, message.author, message.channel)

    async def _punish(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> None:
        """回数に応じてキック+全投稿削除、または BAN する。"""
        cfg = self.bot.config.honeypot
        count = await self.bot.db.increment_strike(guild.id, user.id)
        reason = honeypot_reason(count)
        fields = {
            "ユーザー": f"{user} (`{user.id}`)",
            "罠チャンネル": f"<#{channel.id}>",
            "回数": f"{count} 回目（{cfg.max_kicks + 1} 回目で BAN）",
        }
        # 上限を超えたら BAN する
        if count > cfg.max_kicks:
            try:
                await guild.ban(
                    user, reason=reason, delete_message_seconds=BAN_DELETE_SECONDS
                )
                fields["処置"] = "BAN（直近7日のメッセージ削除）"
            except discord.HTTPException as exc:
                fields["処置"] = f"BAN 失敗: {exc}"
            await self.bot.db.forget_user_messages(guild.id, user.id)
            await self.bot.notifier.notify_emergency(
                make_embed("ハニーポット: BAN", reason, COLOR_DANGER, fields)
            )
            return
        # 追加投稿を防ぐため、先にキックする
        try:
            await guild.kick(user, reason=reason)
            kick_result = "キック"
        except discord.HTTPException as exc:
            kick_result = f"キック失敗: {exc}"
        # そのユーザーの投稿を全削除する
        deleted = await self.purge_user_messages(guild, user.id)
        fields["処置"] = f"{kick_result} / 投稿削除 {deleted} 件"
        await self.bot.notifier.notify_dev(
            make_embed("ハニーポット: キック", reason, COLOR_WARNING, fields)
        )

    # ------------------------------------------------------------
    # 全投稿削除
    # ------------------------------------------------------------
    async def purge_user_messages(self, guild: discord.Guild, user_id: int) -> int:
        """インデックスとチャンネル走査でユーザーの投稿を削除し、件数を返す。"""
        deleted = await self._purge_indexed(guild, user_id)
        deleted += await self._purge_by_scan(guild, user_id)
        await self.bot.db.forget_user_messages(guild.id, user_id)
        return deleted

    async def _purge_indexed(self, guild: discord.Guild, user_id: int) -> int:
        """インデックスに記録された投稿を削除する（高速）。"""
        rows = await self.bot.db.get_user_messages(guild.id, user_id)
        # チャンネルごとにまとめる
        by_channel: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for channel_id, message_id, created_at in rows:
            by_channel[channel_id].append((message_id, created_at))
        now = time.time()
        deleted = 0
        for channel_id, items in by_channel.items():
            channel = guild.get_channel_or_thread(channel_id)
            # 削除済みチャンネル・送信不可チャンネルは飛ばす
            if not isinstance(channel, discord.abc.Messageable):
                continue
            # 14日未満は一括削除、それ以上は1件ずつ削除する
            recent = [
                discord.Object(id=mid)
                for mid, ts in items
                if now - ts < BULK_DELETE_MAX_AGE
            ]
            old = [mid for mid, ts in items if now - ts >= BULK_DELETE_MAX_AGE]
            deleted += await self._bulk_delete(channel, recent)
            for mid in old:
                try:
                    await channel.get_partial_message(mid).delete()
                    deleted += 1
                except discord.HTTPException:
                    continue
        return deleted

    async def _bulk_delete(
        self, channel: discord.abc.Messageable, objects: list[discord.Object]
    ) -> int:
        """100件ずつ一括削除する（失敗時は1件ずつ削除にフォールバック）。"""
        deleted = 0
        for i in range(0, len(objects), BULK_DELETE_CHUNK):
            chunk = objects[i : i + BULK_DELETE_CHUNK]
            try:
                # 1件だけの場合は一括削除 API が使えないので個別削除
                if len(chunk) == 1:
                    await channel.get_partial_message(chunk[0].id).delete()
                else:
                    await channel.delete_messages(chunk)  # type: ignore[attr-defined]
                deleted += len(chunk)
            except discord.HTTPException:
                # 一部が既に消えている等で失敗したら1件ずつ試す
                for obj in chunk:
                    try:
                        await channel.get_partial_message(obj.id).delete()
                        deleted += 1
                    except discord.HTTPException:
                        continue
        return deleted

    async def _purge_by_scan(self, guild: discord.Guild, user_id: int) -> int:
        """各チャンネル履歴を遡って残りの投稿を削除する（ボット起動前の投稿対策）。"""
        days = self.bot.config.honeypot.purge_scan_days
        after = datetime.now(timezone.utc) - timedelta(days=days)
        me = guild.me
        # テキスト・ボイス（テキストチャット）・スレッドを走査対象にする
        channels: list[discord.abc.GuildChannel | discord.Thread] = [
            *guild.text_channels,
            *guild.voice_channels,
            *guild.threads,
        ]
        deleted = 0
        for channel in channels:
            perms = channel.permissions_for(me)
            # 閲覧・履歴・管理権限が揃っていないチャンネルは飛ばす
            if not (
                perms.read_messages
                and perms.read_message_history
                and perms.manage_messages
            ):
                continue
            try:
                removed = await channel.purge(  # type: ignore[union-attr]
                    limit=None,
                    after=after,
                    check=lambda m: m.author.id == user_id,
                    reason="ハニーポット: 全投稿削除",
                )
                deleted += len(removed)
            except discord.HTTPException as exc:
                log.warning("%s の走査削除に失敗しました: %s", channel, exc)
        return deleted


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(HoneypotCog(bot))
