"""OpenAI 互換 embeddings（NVIDIA NIM / LM Studio）。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import openai
from openai import AsyncOpenAI

log = logging.getLogger(__name__)


class EmbedError(Exception):
    """埋め込み API の失敗。"""


class Embedder:
    """passage / query モード対応の埋め込みクライアント。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        batch_size: int = 8,
        rpm: int = 40,
        timeout_sec: float = 120.0,
    ) -> None:
        self.model = model
        self.batch_size = max(1, batch_size)
        self.rpm = max(1, rpm)
        self._min_interval = 60.0 / self.rpm
        self._last_request = 0.0
        self._client = AsyncOpenAI(
            api_key=api_key or "missing",
            base_url=base_url,
            timeout=timeout_sec,
            max_retries=1,
        )
        self._cancel_check: Callable[[], bool] | None = None

    def set_cancel_check(self, check: Callable[[], bool] | None) -> None:
        """キャンセル判定コールバックを設定する。"""
        self._cancel_check = check

    async def close(self) -> None:
        """HTTP クライアントを閉じる。"""
        await self._client.close()

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """文書用埋め込みを返す。"""
        return await self._embed_many(texts, "passage")

    async def embed_query(self, text: str) -> list[float]:
        """検索クエリ用埋め込みを返す。"""
        vectors = await self._embed_many([text], "query")
        return vectors[0]

    async def _embed_many(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]:
        """バッチ分割して埋め込みを取る。"""
        if not texts:
            return []
        if not self._client.api_key or self._client.api_key == "missing":
            raise EmbedError("embedding API key is missing")
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            if self._cancel_check and self._cancel_check():
                raise EmbedError("cancelled")
            batch = texts[i : i + self.batch_size]
            vectors = await self._embed_batch(batch, input_type)
            out.extend(vectors)
        return out

    async def _throttle(self) -> None:
        """RPM 上限に合わせて待機する。"""
        elapsed = time.monotonic() - self._last_request
        wait = self._min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)

    async def _embed_batch(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]:
        """1 リクエスト分の埋め込み。"""
        await self._throttle()
        model = self._model_for(input_type)
        try:
            # NIM は input_type、LM Studio 等は suffix モデル名で通す
            try:
                response = await self._client.embeddings.create(
                    model=self.model,
                    input=texts,
                    extra_body={"input_type": input_type},
                )
            except (openai.BadRequestError, TypeError):
                response = await self._client.embeddings.create(
                    model=model,
                    input=texts,
                )
        except (openai.APIError, openai.APIConnectionError) as exc:
            log.warning("embedding request failed: %s", exc)
            raise EmbedError(str(exc)) from exc
        finally:
            self._last_request = time.monotonic()
        ordered = sorted(response.data, key=lambda d: d.index)
        return [list(item.embedding) for item in ordered]

    def _model_for(self, input_type: str) -> str:
        """OpenAI 互換向けに -passage / -query を付ける。"""
        base = self.model
        if base.endswith(("-passage", "-query")):
            return base
        if "nv-embedqa" in base or "embedqa" in base or "nemotron-embed" in base:
            return f"{base}-{input_type}"
        return base
