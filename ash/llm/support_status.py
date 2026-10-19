"""サポート応答のステータス表示と Components V2 最終メッセージ。"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

import discord
from discord import ui

log = logging.getLogger(__name__)

PHASE_START = "start"
PHASE_ATTACHMENTS = "attachments"
PHASE_RAG = "rag"
PHASE_LLM = "llm"
PHASE_RETRY = "retry"

# 後方互換（日本語）
PHASE_TEXT = {
    PHASE_START: "ちょっと見てみるね…",
    PHASE_ATTACHMENTS: "ログ／出力ファイル、確認してる…",
    PHASE_RAG: "ドキュメントとコードから手がかり探してる…",
    PHASE_LLM: "方針まとめてる…",
    PHASE_RETRY: "うまくいかなかった…30秒あとにもう一回やってみる",
}


class SupportReplyView(ui.LayoutView):
    """本文（プレーン）+ フッター案内／ボタン（アクセント付き）。"""

    def __init__(
        self,
        *,
        body: str,
        footer_hint: str,
        thread_id: int,
        resolve_label: str,
        escalate_label: str,
        enabled: bool = True,
        on_resolve: Callable[[discord.Interaction], Awaitable[None]] | None = None,
        on_escalate: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self._on_resolve = on_resolve
        self._on_escalate = on_escalate

        # 本文はアクセント無し（全体が埋め込みに見えないようにする）
        self.add_item(ui.TextDisplay(body[:3900] or "…"))

        # 案内文 + ボタンだけサイドアクセント付きコンテナにまとめる
        footer = ui.Container(accent_color=0x7AA2F7)
        hint = footer_hint.strip()
        if hint:
            footer.add_item(ui.TextDisplay(hint[:1000]))
        row = ui.ActionRow()
        resolve_btn = ui.Button(
            label=resolve_label,
            style=discord.ButtonStyle.success,
            custom_id=f"ash:support:resolve:{thread_id}",
            disabled=not enabled,
        )
        escalate_btn = ui.Button(
            label=escalate_label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"ash:support:escalate:{thread_id}",
            disabled=not enabled,
        )
        resolve_btn.callback = self._resolve_cb  # type: ignore[method-assign]
        escalate_btn.callback = self._escalate_cb  # type: ignore[method-assign]
        row.add_item(resolve_btn)
        row.add_item(escalate_btn)
        footer.add_item(row)
        self.add_item(footer)

    async def _resolve_cb(self, interaction: discord.Interaction) -> None:
        if self._on_resolve:
            await self._on_resolve(interaction)

    async def _escalate_cb(self, interaction: discord.Interaction) -> None:
        if self._on_escalate:
            await self._on_escalate(interaction)


class SupportBodyOnlyView(ui.LayoutView):
    """過去メッセージ用: 本文のみ（ボタンなし）。"""

    def __init__(self, body: str) -> None:
        super().__init__(timeout=None)
        self.add_item(ui.TextDisplay(body[:3900] or "…"))


class SupportEscalatePendingView(ui.LayoutView):
    """エスカレーション処理中: 本文 + 生成中フッター + 無効ボタン。"""

    def __init__(
        self,
        *,
        body: str,
        thread_id: int,
        resolve_label: str,
        escalate_label: str,
        status_text: str = "サマリー生成中…",
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(ui.TextDisplay(body[:3900] or "…"))
        footer = ui.Container(accent_color=0xE5C07B)
        footer.add_item(ui.TextDisplay((status_text or "サマリー生成中…")[:1000]))
        row = ui.ActionRow()
        row.add_item(
            ui.Button(
                label=resolve_label,
                style=discord.ButtonStyle.success,
                custom_id=f"ash:support:resolve:{thread_id}:busy",
                disabled=True,
            )
        )
        row.add_item(
            ui.Button(
                label=escalate_label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"ash:support:escalate:{thread_id}:busy",
                disabled=True,
            )
        )
        footer.add_item(row)
        self.add_item(footer)


class SupportFailureView(ui.LayoutView):
    """失敗確定: 説明 + 無効化ボタン。"""

    def __init__(
        self,
        *,
        body: str,
        thread_id: int,
        resolve_label: str,
        escalate_label: str,
    ) -> None:
        super().__init__(timeout=None)
        container = ui.Container(accent_color=0xE06C75)
        container.add_item(ui.TextDisplay(body[:3900] or "…"))
        row = ui.ActionRow()
        row.add_item(
            ui.Button(
                label=resolve_label,
                style=discord.ButtonStyle.success,
                custom_id=f"ash:support:resolve:{thread_id}:dead",
                disabled=True,
            )
        )
        row.add_item(
            ui.Button(
                label=escalate_label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"ash:support:escalate:{thread_id}:dead",
                disabled=True,
            )
        )
        container.add_item(row)
        self.add_item(container)


class SupportStatusMessage:
    """処理中は content を edit し、完了後に LayoutView へ差し替える。"""

    def __init__(
        self, origin: discord.Message, *, locale: str = "ja"
    ) -> None:
        self._origin = origin
        self._locale = "en" if locale == "en" else "ja"
        self._message: discord.Message | None = None

    @property
    def message(self) -> discord.Message | None:
        return self._message

    def _phase_label(self, phase: str) -> str:
        from ash.support_i18n import phase_text

        return phase_text(self._locale, phase)

    async def ensure(self) -> discord.Message:
        if self._message is not None:
            return self._message
        self._message = await self._origin.reply(
            self._phase_label(PHASE_START),
            mention_author=False,
        )
        return self._message

    async def set_phase(self, phase: str) -> None:
        text = self._phase_label(phase)
        msg = await self.ensure()
        try:
            await msg.edit(content=text)
        except discord.HTTPException as exc:
            log.warning("support status edit failed: %s", exc)

    async def finalize_success(self, view: SupportReplyView) -> discord.Message:
        msg = await self.ensure()
        await msg.edit(content=None, view=view)
        return msg

    async def finalize_failure(self, view: SupportFailureView) -> discord.Message:
        msg = await self.ensure()
        await msg.edit(content=None, view=view)
        return msg

    async def replace_with_body_only(self, body: str) -> None:
        """前回メッセージからボタンを外す。"""
        if self._message is None:
            return
        try:
            await self._message.edit(content=None, view=SupportBodyOnlyView(body))
        except discord.HTTPException as exc:
            log.warning("support strip buttons failed: %s", exc)


async def strip_message_buttons(message: discord.Message, body: str) -> None:
    """既存メッセージを本文のみ LayoutView にする。"""
    try:
        await message.edit(content=None, view=SupportBodyOnlyView(body))
    except discord.HTTPException as exc:
        log.warning("strip_message_buttons failed: %s", exc)


async def set_escalate_pending(
    message: discord.Message,
    *,
    body: str,
    thread_id: int,
    resolve_label: str,
    escalate_label: str,
    status_text: str = "サマリー生成中…",
) -> None:
    """ボタンを無効化し、生成中フッターを表示する。"""
    view = SupportEscalatePendingView(
        body=body,
        thread_id=thread_id,
        resolve_label=resolve_label,
        escalate_label=escalate_label,
        status_text=status_text,
    )
    try:
        await message.edit(content=None, view=view)
    except discord.HTTPException as exc:
        log.warning("set_escalate_pending failed: %s", exc)
