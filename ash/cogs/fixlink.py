"""FixLink: Twitter/X 等のリンクを埋め込み対応ドメインに置換して返信する。"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

import discord
from discord.ext import commands

from ash.bot import AshBot
from ash.config import FixLinkRule

log = logging.getLogger(__name__)

# 本文中の URL を拾う正規表現
URL_RE = re.compile(r"https?://[^\s<>|`]+", re.IGNORECASE)
# 置換対象から除外する領域（コードブロック・インラインコード・スポイラー・<URL>）
EXCLUDED_RE = re.compile(
    r"```.*?```|`[^`]*`|\|\|.*?\|\||<https?://[^>]+>",
    re.DOTALL | re.IGNORECASE,
)
# URL 末尾に付きがちな句読点・閉じ括弧
TRAILING_PUNCT = ".,!?)]}>'\"、。！？）」』"


def _rewrite(url: str, rules: list[FixLinkRule]) -> str | None:
    """URL がルールに一致すればホストを置換した URL を返す。"""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    for rule in rules:
        # 置換済みドメインはそのまま（再置換しない）
        if host == rule.replace.lower():
            return None
        # ホストが一致しなければ次のルールへ
        if not re.fullmatch(rule.pattern, host, re.IGNORECASE):
            continue
        # パス条件があれば一致するときだけ置換する
        if rule.path_pattern and not re.match(rule.path_pattern, parts.path):
            continue
        return urlunsplit(parts._replace(netloc=rule.replace))
    return None


def find_fixed_links(content: str, rules: list[FixLinkRule]) -> list[str]:
    """本文から置換対象の URL を探し、置換後 URL の一覧を返す。"""
    # 埋め込み抑止・スポイラー・コード内の URL は対象外にする
    visible = EXCLUDED_RE.sub(" ", content)
    fixed: list[str] = []
    for match in URL_RE.finditer(visible):
        # 末尾の句読点を落とす
        url = match.group(0).rstrip(TRAILING_PUNCT)
        new_url = _rewrite(url, rules)
        # 重複を除いて追加する
        if new_url and new_url not in fixed:
            fixed.append(new_url)
    return fixed


class FixLinkCog(commands.Cog):
    """埋め込みが壊れがちなリンクを修正版 URL で返信する。"""

    def __init__(self, bot: AshBot) -> None:
        # Bot 参照を保持する
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """リンクを検出して置換 URL を返信する。"""
        cfg = self.bot.config.fixlink
        # 機能 OFF・サーバー外・ボット・Webhook は対象外
        if not cfg.enabled or message.guild is None:
            return
        if not self.bot.is_guild_allowed(message.guild.id):
            return
        if message.author.bot or message.webhook_id:
            return
        # 除外チャンネル・ハニーポットチャンネルでは動かない
        channel_id = message.channel.id
        if channel_id in cfg.ignore_channel_ids:
            return
        if channel_id in self.bot.config.honeypot.channel_ids:
            return
        links = find_fixed_links(message.content, cfg.rules)
        if not links:
            return
        # まとめて1通で返信する（通知は飛ばさない）
        try:
            await message.reply(
                "\n".join(links),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            log.warning("FixLink の返信に失敗しました: %s", exc)
            return
        # 元投稿の埋め込みを非表示にする（権限が無ければ諦める）
        if cfg.suppress_original_embed:
            try:
                await message.edit(suppress=True)
            except discord.HTTPException:
                pass


async def setup(bot: AshBot) -> None:
    """Cog を登録する。"""
    await bot.add_cog(FixLinkCog(bot))
