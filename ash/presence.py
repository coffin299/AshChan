"""Discord プレゼンス（カスタムステータス）のローテーション。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ash.bot import AshBot
    from ash.config import PresenceConfig

log = logging.getLogger(__name__)

# Discord の activity name 上限に余裕を持たせる
_MAX_NAME = 120


class PresenceManager:
    """優先度付きでステータス文言を切り替え・ローテーションする。"""

    def __init__(self, bot: AshBot) -> None:
        self._bot = bot
        self._task: asyncio.Task[None] | None = None
        self._index = 0
        self._last_text = ""
        # thread_id -> フォーラム表示名
        self._support_forums: dict[int, str] = {}
        # None | "rebuild-nim" | "sync"
        self._rag_kind: str | None = None
        self._chat_busy = 0

    @property
    def _cfg(self) -> PresenceConfig:
        return self._bot.config.presence

    def start(self) -> None:
        """ローテーションループを開始する。"""
        if not self._cfg.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="presence-rotate")

    async def stop(self) -> None:
        """ループを止めてステータスを消す。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._bot.is_ready():
            try:
                await self._bot.change_presence(activity=None)
            except discord.HTTPException:
                pass

    def begin_support(self, thread_id: int, forum_name: str) -> None:
        """サポート応答開始を記録する。"""
        name = (forum_name or "forum").strip() or "forum"
        self._support_forums[thread_id] = name[:40]
        self._index = 0

    def end_support(self, thread_id: int) -> None:
        """サポート応答終了を記録する。"""
        self._support_forums.pop(thread_id, None)

    def set_rag(self, kind: str | None) -> None:
        """RAG ジョブ中の表示（None で解除）。"""
        self._rag_kind = kind
        self._index = 0

    def begin_chat(self) -> None:
        """通常会話の生成開始。"""
        self._chat_busy += 1

    def end_chat(self) -> None:
        """通常会話の生成終了。"""
        self._chat_busy = max(0, self._chat_busy - 1)

    def _candidates(self) -> list[str]:
        """いま出すべきステータス候補（優先度順・ローテ対象）。"""
        cfg = self._cfg
        if self._rag_kind == "rebuild-nim":
            return [cfg.rag_rebuild]
        if self._rag_kind == "sync":
            return [cfg.rag_sync]
        if self._rag_kind:
            return [cfg.rag_sync]
        if self._support_forums:
            # フォーラム名ごとに1行。同じ名前はまとめる
            names = sorted(set(self._support_forums.values()))
            template = cfg.support_template or "サポート中/{forum}"
            return [template.replace("{forum}", n) for n in names]
        if self._chat_busy > 0:
            return [cfg.chat_busy]
        idle = [s.strip() for s in cfg.idle_statuses if str(s).strip()]
        return idle or ["通常待機"]

    async def _loop(self) -> None:
        """一定間隔で候補をローテして presence を更新する。"""
        await self._bot.wait_until_ready()
        while not self._bot.is_closed():
            cfg = self._cfg
            if not cfg.enabled:
                await asyncio.sleep(5.0)
                continue
            candidates = self._candidates()
            if not candidates:
                candidates = ["通常待機"]
            text = candidates[self._index % len(candidates)]
            self._index = (self._index + 1) % max(len(candidates), 1)
            await self._apply(text)
            interval = max(5.0, float(cfg.rotate_sec))
            await asyncio.sleep(interval)

    async def _apply(self, text: str) -> None:
        """同じ文言の連続更新を避けて change_presence する。"""
        name = text.strip()[:_MAX_NAME]
        if not name or name == self._last_text:
            return
        if not self._bot.is_ready():
            return
        try:
            await self._bot.change_presence(
                activity=discord.CustomActivity(name=name)
            )
            self._last_text = name
        except discord.HTTPException as exc:
            log.warning("presence update failed: %s", exc)

    async def bump(self) -> None:
        """状態変化直後にすぐ反映したいとき用。"""
        candidates = self._candidates()
        if candidates:
            self._index = 0
            await self._apply(candidates[0])
