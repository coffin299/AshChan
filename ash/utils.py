"""複数の Cog で共用する小さなユーティリティ。"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

import discord


@lru_cache(maxsize=64)
def compile_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """正規表現リストをコンパイルしてキャッシュする（不正なものは捨てる）。"""
    compiled = []
    # 1件ずつコンパイルし、壊れたパターンで全体が止まらないようにする
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return tuple(compiled)


def has_any_role(member: discord.abc.User, role_ids: Iterable[int]) -> bool:
    """メンバーが指定ロールのいずれかを持っているか判定する。"""
    # User（サーバー外）はロールを持たない
    if not isinstance(member, discord.Member):
        return False
    # 集合演算で高速に判定する
    wanted = set(role_ids)
    return any(role.id in wanted for role in member.roles)


def is_privileged(member: discord.abc.User) -> bool:
    """サーバーオーナーまたは管理者権限持ちかを判定する。"""
    # Member でなければ権限は判定できない
    if not isinstance(member, discord.Member):
        return False
    # オーナーは常に特権扱い
    if member.guild.owner_id == member.id:
        return True
    # 管理者権限を持っていれば特権扱い
    return member.guild_permissions.administrator


def truncate(text: str, limit: int) -> str:
    """埋め込みのフィールド等に収まるよう文字列を切り詰める。"""
    # 収まるならそのまま返す
    if len(text) <= limit:
        return text
    # 末尾を省略記号に置き換える
    return text[: max(limit - 1, 0)] + "…"
