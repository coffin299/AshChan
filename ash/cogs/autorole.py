"""自動ロール: 新規参加者に指定ロールを付与する。"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.notifier import COLOR_WARNING, make_embed

log = logging.getLogger(__name__)


class AutoRoleCog(commands.Cog):
    """新規参加メンバーへのロール自動付与。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """参加時にロールを付与する（メンバーシップ審査待ちなら保留）。"""
        cfg = self.bot.config.autorole
        # 機能 OFF なら何もしない
        if not cfg.enabled:
            return
        # 審査（ルール同意）待ちの人間は、通過時に付与する
        if cfg.wait_for_screening and member.pending and not member.bot:
            return
        await self._assign(member)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """メンバーシップ審査を通過したタイミングでロールを付与する。"""
        cfg = self.bot.config.autorole
        # 機能 OFF・審査待ちを使わない設定なら何もしない
        if not cfg.enabled or not cfg.wait_for_screening:
            return
        # pending が True → False に変わったときだけ付与する
        if before.pending and not after.pending:
            await self._assign(after)

    async def _assign(self, member: discord.Member) -> None:
        """設定されたロールのうち付与可能なものを付与する。"""
        cfg = self.bot.config.autorole
        guild = member.guild
        # ボットと人間で付与するロールを切り替える
        role_ids = cfg.bot_role_ids if member.bot else cfg.role_ids
        if not role_ids:
            return
        me = guild.me
        roles: list[discord.Role] = []
        problems: list[str] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            # 存在しないロール ID は設定ミスとして記録する
            if role is None:
                problems.append(f"ロール `{role_id}` が見つかりません")
                continue
            # ボットより上位のロール・管理ロールは付与できない
            if role >= me.top_role or role.managed:
                problems.append(f"@{role.name} はボットより上位か管理ロールです")
                continue
            # 既に持っているロールは飛ばす
            if role not in member.roles:
                roles.append(role)
        if roles:
            try:
                await member.add_roles(*roles, reason="AshChan: 自動ロール付与")
            except discord.HTTPException as exc:
                problems.append(f"付与に失敗: {exc}")
        # 問題があれば dev 通知する（成功時は通知しない）
        if problems:
            log.warning("自動ロール付与の問題 (%s): %s", member, problems)
            await self.bot.notifier.notify_dev(
                make_embed(
                    "自動ロール付与の問題",
                    f"{member.mention} へのロール付与で問題がありました。",
                    COLOR_WARNING,
                    {
                        "ユーザー": f"{member} (`{member.id}`)",
                        "内容": "\n".join(problems),
                    },
                )
            )


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(AutoRoleCog(bot))
