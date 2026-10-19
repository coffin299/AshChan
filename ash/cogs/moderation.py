"""モデレーション: ルールベース検知（削除+タイムアウト）と AutoMod ルール同期。"""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.config import ModerationConfig
from ash.notifier import COLOR_SUCCESS, COLOR_WARNING, make_embed
from ash.utils import compile_patterns, has_any_role, is_privileged, truncate

log = logging.getLogger(__name__)

# ボットが管理する AutoMod ルール名
RULE_KEYWORD = "[Ash] NGワード"
RULE_MENTION = "[Ash] メンションスパム"
RULE_SPAM = "[Ash] スパム疑い"
# AutoMod のキーワード上限（Discord 側の制約）
AUTOMOD_MAX_KEYWORDS = 1000
AUTOMOD_MAX_KEYWORD_LEN = 60
# AutoMod のメンション上限の最大値（Discord 側の制約）
AUTOMOD_MAX_MENTION_LIMIT = 50
# AutoMod ブロック時にユーザーへ表示する文言
AUTOMOD_BLOCK_MESSAGE = "このメッセージはサーバーのルールによりブロックされました。"


def detect_violation(message: discord.Message, cfg: ModerationConfig) -> str | None:
    """メッセージの違反理由を返す（違反なしなら None）。"""
    content = message.content
    # メンション爆撃: ユーザー + ロール + @everyone/@here の合計で判定する
    if cfg.mention_limit > 0:
        count = len(message.mentions) + len(message.role_mentions)
        count += 1 if message.mention_everyone else 0
        if count > cfg.mention_limit:
            return f"メンション過多（{count}件）"
    # 招待リンク
    if cfg.block_invites:
        for pattern in compile_patterns(tuple(cfg.invite_patterns)):
            if pattern.search(content):
                return "招待リンク"
    # 疑わしいリンク
    if cfg.block_suspicious_links:
        for pattern in compile_patterns(tuple(cfg.suspicious_link_patterns)):
            if pattern.search(content):
                return f"疑わしいリンク（{pattern.pattern}）"
    # NGワード（部分一致・大文字小文字無視）
    lowered = content.casefold()
    for word in cfg.ng_words:
        if word and word.casefold() in lowered:
            return f"NGワード（{word}）"
    return None


class ModerationCog(commands.Cog):
    """ルールベースのモデレーションと AutoMod 同期。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # AutoMod 同期を初回 on_ready で一度だけ行うためのフラグ
        self._synced_once = False

    # ------------------------------------------------------------
    # ルールベース検知
    # ------------------------------------------------------------
    def _is_exempt(self, member: discord.abc.User) -> bool:
        """モデレーション対象外かどうか。"""
        cfg = self.bot.config.moderation
        # オーナー・管理者・除外ロール持ちは対象外
        return is_privileged(member) or has_any_role(member, cfg.exempt_role_ids)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """新規メッセージを検査する。"""
        await self._inspect(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """編集で違反内容に書き換えられた場合も検査する。"""
        # 本文が変わっていなければ（埋め込み追加等）検査しない
        if before.content == after.content:
            return
        await self._inspect(after)

    async def _inspect(self, message: discord.Message) -> None:
        """違反判定と処置。"""
        cfg = self.bot.config.moderation
        # 機能 OFF・サーバー外・ボット・Webhook は対象外
        if not cfg.enabled or message.guild is None:
            return
        if not self.bot.is_guild_allowed(message.guild.id):
            return
        if message.author.bot or message.webhook_id:
            return
        # ハニーポットチャンネルはハニーポット側で処理する
        if message.channel.id in self.bot.config.honeypot.channel_ids:
            return
        # 除外対象なら何もしない
        if self._is_exempt(message.author):
            return
        reason = detect_violation(message, cfg)
        if reason is None:
            return
        await self._punish(message, reason, cfg)

    async def _punish(
        self, message: discord.Message, reason: str, cfg: ModerationConfig
    ) -> None:
        """メッセージ削除とタイムアウトを行い、dev 通知する。"""
        actions: list[str] = []
        # 違反メッセージを削除する
        try:
            await message.delete()
            actions.append("削除")
        except discord.HTTPException as exc:
            log.warning("違反メッセージの削除に失敗しました: %s", exc)
        # タイムアウトする（0分なら削除のみ）
        member = message.author
        if cfg.timeout_minutes > 0 and isinstance(member, discord.Member):
            try:
                await member.timeout(
                    timedelta(minutes=cfg.timeout_minutes),
                    reason=f"モデレーション: {reason}",
                )
                actions.append(f"タイムアウト{cfg.timeout_minutes}分")
            except discord.HTTPException as exc:
                log.warning("タイムアウトに失敗しました: %s", exc)
        # 処置内容を dev 通知する
        await self.bot.notifier.notify_dev(
            make_embed(
                "モデレーション処置",
                f"{member.mention} の投稿を処置しました。",
                COLOR_WARNING,
                {
                    "ユーザー": f"{member} (`{member.id}`)",
                    "チャンネル": f"<#{message.channel.id}>",
                    "理由": reason,
                    "処置": " / ".join(actions) or "失敗（権限不足の可能性）",
                    "内容": truncate(message.content, 1000),
                },
            )
        )

    # ------------------------------------------------------------
    # AutoMod 同期
    # ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """初回接続時に全サーバーの AutoMod ルールを同期する。"""
        # 再接続のたびに同期しない
        if self._synced_once:
            return
        self._synced_once = True
        await self.sync_all_automod()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """新しく参加したサーバーにも AutoMod ルールを作る。"""
        if not self.bot.is_guild_allowed(guild.id):
            return
        await self.sync_automod(guild)

    async def sync_all_automod(self) -> None:
        """参加中の全サーバーで AutoMod ルールを同期する。"""
        for guild in self.bot.guilds:
            if not self.bot.is_guild_allowed(guild.id):
                continue
            await self.sync_automod(guild)

    async def sync_automod(self, guild: discord.Guild) -> None:
        """1サーバー分の [Ash] AutoMod ルールを作成/更新する。"""
        cfg = self.bot.config.moderation
        # 連携 OFF なら既存ルールには触れない
        if not cfg.automod.enabled:
            return
        if not self.bot.is_guild_allowed(guild.id):
            return
        # サーバー管理権限が無ければ同期できない
        if not guild.me.guild_permissions.manage_guild:
            log.warning("%s: サーバー管理権限が無いため AutoMod を同期できません", guild)
            return
        try:
            all_rules = list(await guild.fetch_automod_rules())
        except discord.HTTPException as exc:
            log.warning("%s: AutoMod ルールの取得に失敗しました: %s", guild, exc)
            return
        existing_by_name = {r.name: r for r in all_rules}
        # mention_spam / spam 等はタイプあたり1件までのサーバーがある
        existing_by_type: dict[discord.AutoModRuleTriggerType, discord.AutoModRule] = {}
        for rule in all_rules:
            existing_by_type.setdefault(rule.trigger.type, rule)

        # 除外ロールは AutoMod 側にも反映する
        exempt_roles = [discord.Object(id=rid) for rid in cfg.exempt_role_ids]
        # NGワードを AutoMod の制約内に収め、部分一致用にワイルドカードで囲む
        keywords = [
            f"*{w}*"
            for w in cfg.ng_words
            if w and len(w) + 2 <= AUTOMOD_MAX_KEYWORD_LEN
        ][:AUTOMOD_MAX_KEYWORDS]
        # ルールごとの (名前, トリガー, 有効か) を定義する
        specs = [
            (
                RULE_KEYWORD,
                discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword,
                    keyword_filter=keywords or ["*__ash_placeholder__*"],
                ),
                cfg.automod.keyword_rule and bool(keywords),
            ),
            (
                RULE_MENTION,
                discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.mention_spam,
                    mention_limit=min(
                        max(cfg.mention_limit, 1), AUTOMOD_MAX_MENTION_LIMIT
                    ),
                ),
                cfg.automod.mention_spam_rule and cfg.mention_limit > 0,
            ),
            (
                RULE_SPAM,
                discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam),
                cfg.automod.spam_rule,
            ),
        ]
        # サーバーあたり1件までのタイプ（それ以外は名前一致のみ）
        singleton_types = {
            discord.AutoModRuleTriggerType.mention_spam,
            discord.AutoModRuleTriggerType.spam,
        }
        results: list[str] = []
        for name, trigger, enabled in specs:
            rule = existing_by_name.get(name)
            if rule is None and trigger.type in singleton_types:
                # 同タイプの既存ルールを流用（MAX_RULES_OF_TYPE 対策）
                rule = existing_by_type.get(trigger.type)
            results.append(
                await self._upsert_rule(
                    guild, rule, name, trigger, enabled, exempt_roles
                )
            )
        # 同期結果は非公開側へ
        await self.bot.notifier.notify_dev(
            make_embed(
                "AutoMod 同期",
                f"{guild.name} の AutoMod ルールを同期しました。",
                COLOR_SUCCESS,
                {"結果": "\n".join(results)},
            )
        )

    async def _upsert_rule(
        self,
        guild: discord.Guild,
        rule: discord.AutoModRule | None,
        name: str,
        trigger: discord.AutoModTrigger,
        enabled: bool,
        exempt_roles: list[discord.Object],
    ) -> str:
        """AutoMod ルールを作成または更新し、結果の文字列を返す。"""
        actions = [discord.AutoModRuleAction(custom_message=AUTOMOD_BLOCK_MESSAGE)]
        try:
            if rule is None:
                if not enabled:
                    return f"{name}: 無効（未作成）"
                await guild.create_automod_rule(
                    name=name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=True,
                    exempt_roles=exempt_roles,
                    reason="AshChan: AutoMod 同期",
                )
                return f"{name}: 作成"
            if not enabled:
                await rule.edit(enabled=False, reason="AshChan: AutoMod 同期")
                return f"{name}: 無効化"
            # 既存（名前違いの同タイプ含む）を Ash 設定で更新。可能なら名前も揃える
            edit_kwargs: dict = {
                "trigger": trigger,
                "actions": actions,
                "enabled": True,
                "exempt_roles": exempt_roles,
                "reason": "AshChan: AutoMod 同期",
            }
            renamed = rule.name != name
            if renamed:
                edit_kwargs["name"] = name
            await rule.edit(**edit_kwargs)
            if renamed:
                return f"{name}: 更新（既存ルール流用）"
            return f"{name}: 更新"
        except discord.HTTPException as exc:
            # 作成時に枠が埋まっていた場合は同タイプを探して更新を再試行
            if (
                rule is None
                and enabled
                and "AUTO_MODERATION_MAX_RULES_OF_TYPE_EXCEEDED" in str(exc)
            ):
                try:
                    for existing in await guild.fetch_automod_rules():
                        if existing.trigger.type == trigger.type:
                            return await self._upsert_rule(
                                guild,
                                existing,
                                name,
                                trigger,
                                enabled,
                                exempt_roles,
                            )
                except discord.HTTPException:
                    pass
            log.warning("%s: AutoMod ルール %s の同期に失敗: %s", guild, name, exc)
            return f"{name}: 失敗（{exc.text or exc}）"


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(ModerationCog(bot))
