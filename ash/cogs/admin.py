"""管理用スラッシュコマンド（/ash status, /ash reload, /ash honeypot ...）。"""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from ash.bot import AshBot
from ash.config import ConfigError
from ash.notifier import COLOR_INFO, COLOR_SUCCESS, make_embed


def _on_off(value: bool) -> str:
    """真偽値を ON/OFF 表記にする。"""
    return "ON" if value else "OFF"


def _format_uptime(seconds: float) -> str:
    """稼働時間を「X日 HH:MM:SS」形式にする。"""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}日 {hours:02}:{minutes:02}:{secs:02}"


class AdminCog(commands.Cog):
    """管理者向けコマンド群。"""

    # /ash グループ（管理者権限のみ表示・実行可、サーバー内限定）
    ash = app_commands.Group(
        name="ash",
        description="アッシュの管理コマンド",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    # /ash honeypot サブグループ
    honeypot = app_commands.Group(
        name="honeypot", description="ハニーポット回数の管理", parent=ash
    )

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot

    def _github_status(self) -> str:
        """GitHub Webhook サーバーの状態を文字列にする。"""
        cfg = self.bot.config.github
        # 機能 OFF ならそれだけ表示する
        if not cfg.enabled:
            return "OFF"
        github = self.bot.get_cog("GitHubCog")
        running = bool(github and github.is_running)  # type: ignore[attr-defined]
        state = "待受中" if running else "停止（secret/ポートを確認）"
        return f"ON / {state} / {cfg.host}:{cfg.port}{cfg.path}"

    @ash.command(name="status", description="稼働状況と LLM の疎通を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        """稼働状況を表示する。"""
        # 疎通確認に時間がかかるので先に応答を保留する
        await interaction.response.defer(ephemeral=True)
        cfg = self.bot.config
        llm_ok = await self.bot.llm.ping()
        embed = make_embed(
            "アッシュの状態",
            color=COLOR_INFO,
            fields={
                "稼働時間": _format_uptime(time.time() - self.bot.started_at),
                "レイテンシ": f"{self.bot.latency * 1000:.0f} ms",
                "LLM": f"{cfg.llm.model} / {'疎通OK' if llm_ok else '疎通NG'}",
                "Web検索": _on_off(cfg.search.enabled),
                "モデレーション": _on_off(cfg.moderation.enabled),
                "AutoMod連携": _on_off(cfg.moderation.automod.enabled),
                "アンチnuke": _on_off(cfg.antinuke.enabled),
                "ハニーポット": (
                    f"{_on_off(cfg.honeypot.enabled)}"
                    f"（罠 {len(cfg.honeypot.channel_ids)} ch）"
                ),
                "FixLink": _on_off(cfg.fixlink.enabled),
                "自動ロール": (
                    f"{_on_off(cfg.autorole.enabled)}"
                    f"（{len(cfg.autorole.role_ids)} ロール）"
                ),
                "GitHub通知": self._github_status(),
            },
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ash.command(name="reload", description="config.yaml とプロンプトを再読込します")
    async def reload(self, interaction: discord.Interaction) -> None:
        """設定とプロンプトを再読込し、AutoMod を再同期する。"""
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.reload_config()
        except (ConfigError, OSError, ValueError, TypeError) as exc:
            # 設定ミスは内容をそのまま伝える（既存設定は維持される）
            await interaction.followup.send(
                f"再読込に失敗しちゃった: {exc}", ephemeral=True
            )
            return
        # AutoMod ルールを新しい設定で同期し直す
        moderation = self.bot.get_cog("ModerationCog")
        if moderation is not None:
            await moderation.sync_all_automod()  # type: ignore[attr-defined]
        # GitHub Webhook サーバーを新しい設定（ポート等）で作り直す
        github = self.bot.get_cog("GitHubCog")
        if github is not None:
            await github.restart_server()  # type: ignore[attr-defined]
        await interaction.followup.send("設定を読み直したよ。", ephemeral=True)
        await self.bot.notifier.notify_dev(
            make_embed(
                "設定再読込",
                f"{interaction.user} が設定を再読込しました。",
                COLOR_SUCCESS,
            )
        )

    @honeypot.command(name="count", description="ユーザーのハニーポット検知回数を表示します")
    @app_commands.describe(user="対象ユーザー")
    async def honeypot_count(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """ハニーポット回数を表示する。"""
        assert interaction.guild is not None
        count = await self.bot.db.get_strike(interaction.guild.id, user.id)
        max_kicks = self.bot.config.honeypot.max_kicks
        await interaction.response.send_message(
            f"{user.mention} のハニーポット検知回数: **{count}** 回"
            f"（{max_kicks + 1} 回目で BAN）",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @honeypot.command(name="reset", description="ユーザーのハニーポット検知回数を0に戻します")
    @app_commands.describe(user="対象ユーザー")
    async def honeypot_reset(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """ハニーポット回数をリセットする。"""
        assert interaction.guild is not None
        await self.bot.db.reset_strike(interaction.guild.id, user.id)
        await interaction.response.send_message(
            f"{user.mention} のハニーポット検知回数をリセットしたよ。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.notifier.notify_dev(
            make_embed(
                "ハニーポット回数リセット",
                f"{interaction.user} が {user} (`{user.id}`) の回数をリセットしました。",
                COLOR_INFO,
            )
        )


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(AdminCog(bot))
