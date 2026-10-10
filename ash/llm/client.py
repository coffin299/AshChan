"""NVIDIA NIM（OpenAI 互換 API）クライアント: ストリーミングと Tool Calling。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import openai
from openai import AsyncOpenAI

from ash.config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 呼び出しの失敗（タイムアウト・429・5xx・接続断など）。"""


@dataclass
class ToolCall:
    """モデルが要求したツール呼び出し1件。"""

    id: str
    name: str
    arguments: str


@dataclass
class StreamEvent:
    """ストリーミング中に発生するイベント。"""

    # "text"（本文の差分）または "tool_calls"（ツール呼び出し要求）
    kind: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient:
    """NIM へのチャット補完リクエストを扱う。"""

    def __init__(self, config: LLMConfig) -> None:
        # 設定を保持する
        self._config = config
        # OpenAI 互換クライアントを生成する（リトライは1回だけ）
        self._client = AsyncOpenAI(
            api_key=config.api_key or "missing",
            base_url=config.base_url,
            timeout=config.timeout_sec,
            max_retries=1,
        )

    @property
    def model(self) -> str:
        """使用中のモデル名。"""
        return self._config.model

    async def close(self) -> None:
        """HTTP クライアントを閉じる。"""
        await self._client.close()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """チャット補完をストリーミングし、本文差分とツール呼び出しを順に返す。"""
        # API キー未設定は即エラーにする
        if not self._config.api_key:
            raise LLMError("llm.api_key が未設定です")
        # リクエストパラメータを組み立てる
        params: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": True,
        }
        # ツールがある場合のみ tools を渡す
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        # index ごとにツール呼び出しの断片を連結するバッファ
        pending: dict[int, dict[str, str]] = {}
        try:
            stream = await self._client.chat.completions.create(**params)
            async for chunk in stream:
                # choices が空のチャンク（usage 等）は読み飛ばす
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 本文の差分があれば即座に流す
                if delta.content:
                    yield StreamEvent(kind="text", text=delta.content)
                # ツール呼び出しの断片を index 単位で連結する
                for tc in delta.tool_calls or []:
                    slot = pending.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    # id と name は最初の断片にだけ入っていることが多い
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    # 引数 JSON は分割されて届くので連結する
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        except (openai.APIError, openai.APIConnectionError) as exc:
            # 失敗内容はローカルログだけに残す
            log.warning("NIM へのリクエストに失敗しました: %s", exc)
            raise LLMError(str(exc)) from exc

        # ストリーム終了後、溜まったツール呼び出しをまとめて返す
        if pending:
            calls = [
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
                for index, slot in sorted(pending.items())
                if slot["name"]
            ]
            if calls:
                yield StreamEvent(kind="tool_calls", tool_calls=calls)

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        """ツールなしで生成し、全文をまとめて返す（短文生成用）。"""
        parts: list[str] = []
        # ストリーミングの本文差分を連結する
        async for event in self.stream_chat(messages):
            if event.kind == "text":
                parts.append(event.text)
        return "".join(parts).strip()

    async def ping(self) -> bool:
        """API の疎通確認（モデル一覧取得が成功するか）。"""
        # キー未設定なら疎通不可
        if not self._config.api_key:
            return False
        try:
            await self._client.models.list()
            return True
        except (openai.APIError, openai.APIConnectionError):
            return False
