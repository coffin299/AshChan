"""クエリ埋め込みによる類似チャンク検索。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ash.rag.embedder import Embedder
from ash.rag.store import RagStore, StoredChunk


@dataclass
class RagHit:
    """検索ヒット1件。"""

    path: str
    chunk_index: int
    text: str
    score: float
    release_tag: str


class RagRetriever:
    """コサイン類似度の top-k 検索。"""

    def __init__(self, store: RagStore, embedder: Embedder, top_k: int = 8) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = max(1, top_k)
        self._cache: list[StoredChunk] | None = None
        self._matrix: np.ndarray | None = None

    def invalidate(self) -> None:
        """インデックス更新後にキャッシュを捨てる。"""
        self._cache = None
        self._matrix = None

    async def retrieve(self, query: str) -> list[RagHit]:
        """クエリに近いチャンクを返す。"""
        q = query.strip()
        if not q:
            return []
        await self._ensure_cache()
        if not self._cache or self._matrix is None or self._matrix.size == 0:
            return []
        vector = np.asarray(await self.embedder.embed_query(q), dtype=np.float32)
        qn = float(np.linalg.norm(vector))
        if qn == 0.0:
            return []
        # 行正規化済み行列との内積 = コサイン類似度
        scores = self._matrix @ (vector / qn)
        k = min(self.top_k, len(scores))
        # ドキュメントをわずかに優先
        adjusted = scores.copy()
        for i, chunk in enumerate(self._cache):
            lower = chunk.path.lower()
            if lower.endswith((".md", ".txt")) or lower.startswith("__release__/"):
                adjusted[i] += 0.02
        top_idx = np.argpartition(-adjusted, k - 1)[:k]
        top_idx = top_idx[np.argsort(-adjusted[top_idx])]
        hits: list[RagHit] = []
        for i in top_idx:
            chunk = self._cache[int(i)]
            hits.append(
                RagHit(
                    path=chunk.path,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    score=float(adjusted[int(i)]),
                    release_tag=chunk.release_tag,
                )
            )
        return hits

    async def _ensure_cache(self) -> None:
        if self._cache is not None and self._matrix is not None:
            return
        chunks = await self.store.load_all_embeddings()
        self._cache = chunks
        if not chunks:
            self._matrix = np.zeros((0, 0), dtype=np.float32)
            return
        mat = np.stack([c.embedding for c in chunks]).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        self._matrix = mat / norms

    @staticmethod
    def format_context(hits: list[RagHit], *, max_chars: int = 12_000) -> str:
        """LLM に渡すコンテキスト文字列。"""
        if not hits:
            return ""
        parts: list[str] = []
        used = 0
        for hit in hits:
            block = (
                f"[source: {hit.path} #{hit.chunk_index} "
                f"(tag={hit.release_tag}, score={hit.score:.3f})]\n{hit.text}"
            )
            if used + len(block) + 2 > max_chars:
                break
            parts.append(block)
            used += len(block) + 2
        return "\n\n---\n\n".join(parts)
