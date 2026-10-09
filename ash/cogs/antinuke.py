"""アンチnuke: 監査ログを実行者単位で集計し、閾値超過を通知する（処罰はしない）。"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.notifier import COLOR_DANGER, make_embed

log = logging.getLogger(__name__)

# 監査ログのアクション → 集計キーの対応表
ACTION_KEYS: dict[discord.AuditLogAction, str] = {
    discord.AuditLogAction.channel_create: "channel_create",
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.role_create: "role_create",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.kick: "kick",
    discord.AuditLogAction.webhook_create: "webhook_create",
}
# 集計キーの日本語表示名
ACTION_LABELS: dict[str, str] = {
    "channel_create": "チャンネル作成",
    "channel_delete": "チャンネル削除",
    "role_create": "ロール作成",
    "role_delete": "ロール削除",
    "ban": "BAN",
    "kick": "キック",
    "webhook_create": "Webhook作成",
    "dangerous_role_grant": "危険権限の付与",
}
# 危険とみなす権限
DANGEROUS_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "mention_everyone",
)


def dangerous_flags(perms: discord.Permissions | None) -> set[str]:
    """権限オブジェクトから危険権限の名前集合を返す。"""
    # 権限情報が無ければ空集合
    if perms is None:
        return set()
    return {name for name in DANGEROUS_PERMISSIONS if getattr(perms, name, False)}


class AntiNukeCog(commands.Cog):
    """監査ログを監視し、破壊的操作の連続を通知する。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # (guild_id, actor_id, action) → 操作時刻のキュー
        self._events: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        # (guild_id, actor_id, action) → 最終通知時刻
        self._last_alert: dict[tuple[int, int, str], float] = {}

    def _record(self, key: tuple[int, int, str]) -> int:
        """操作を記録し、時間窓内の件数を返す（スライディングウィンドウ）。"""
        now = time.monotonic()
        queue = self._events[key]
        queue.append(now)
        # 時間窓より古い記録を捨てる
        cutoff = now - self.bot.config.antinuke.window_seconds
        while queue and queue[0] < cutoff:
            queue.popleft()
        return len(queue)

    def _is_ignored(self, guild: discord.Guild, actor_id: int) -> bool:
        """監視対象外の実行者か判定する。"""
        # 自分自身・オーナー・ホワイトリストは対象外
        me = self.bot.user
        if me is not None and actor_id == me.id:
            return True
        if actor_id == guild.owner_id:
            return True
        return actor_id in self.bot.config.antinuke.whitelist_user_ids

    def _classify(self, entry: discord.AuditLogEntry) -> tuple[str, str] | None:
        """監査ログを (集計キー, 詳細) に分類する（対象外なら None）。"""
        # 単純なアクションは対応表で分類する
        key = ACTION_KEYS.get(entry.action)
        if key is not None:
            return key, str(entry.target or "-")
        guild = entry.guild
        # メンバーへのロール付与: 付与ロールに危険権限があるか
        if entry.action is discord.AuditLogAction.member_role_update:
            added = getattr(entry.after, "roles", None) or []
            for role_ref in added:
                role = guild.get_role(role_ref.id)
                flags = dangerous_flags(role.permissions if role else None)
                if role is not None and flags:
                    perms_text = ", ".join(sorted(flags))
                    detail = f"{entry.target} に @{role.name} を付与（{perms_text}）"
                    return "dangerous_role_grant", detail
            return None
        # ロール更新: 危険権限が新たに追加されたか
        if entry.action is discord.AuditLogAction.role_update:
            before = dangerous_flags(getattr(entry.before, "permissions", None))
            after = dangerous_flags(getattr(entry.after, "permissions", None))
            gained = after - before
            if gained:
                detail = f"@{entry.target} に {', '.join(sorted(gained))} を追加"
                return "dangerous_role_grant", detail
        return None

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """監査ログの新規エントリを集計する。"""
        cfg = self.bot.config.antinuke
        # 機能 OFF なら何もしない
        if not cfg.enabled:
            return
        guild = entry.guild
        actor_id = entry.user_id
        # 実行者不明・対象外の実行者は無視する
        if actor_id is None or self._is_ignored(guild, actor_id):
            return
        classified = self._classify(entry)
        if classified is None:
            return
        action, detail = classified
        # 閾値 0 以下・未設定の操作は監視しない
        threshold = cfg.thresholds.get(action, 0)
        if threshold <= 0:
            return
        key = (guild.id, actor_id, action)
        count = self._record(key)
        # 閾値未満なら通知しない
        if count < threshold:
            return
        # 同じ実行者・操作の通知はクールダウン中なら抑止する
        now = time.monotonic()
        if now - self._last_alert.get(key, -1e9) < cfg.alert_cooldown_seconds:
            return
        self._last_alert[key] = now
        await self._alert(guild, actor_id, action, count, detail)

    async def _alert(
        self,
        guild: discord.Guild,
        actor_id: int,
        action: str,
        count: int,
        detail: str,
    ) -> None:
        """dev 通知と緊急 DM を送る。"""
        cfg = self.bot.config.antinuke
        member = guild.get_member(actor_id)
        actor_text = f"{member} (`{actor_id}`)" if member else f"`{actor_id}`"
        log.warning(
            "Anti-nuke: %s / actor=%s / %s x%d", guild, actor_id, action, count
        )
        await self.bot.notifier.notify_emergency(
            make_embed(
                "🚨 アンチnuke検知",
                f"**{guild.name}** で破壊的な操作が連続しています。至急確認してください。",
                COLOR_DANGER,
                {
                    "実行者": f"<@{actor_id}> {actor_text}",
                    "操作": ACTION_LABELS.get(action, action),
                    "件数": f"{count} 件 / {cfg.window_seconds} 秒",
                    "直近の対象": detail,
                },
            )
        )


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(AntiNukeCog(bot))
