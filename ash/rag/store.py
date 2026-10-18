"""RAG チャンクの SQLite 永続化。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite
import numpy as np

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    release_tag TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    UNIQUE(path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_rag_path ON rag_chunks(path);
CREATE INDEX IF NOT EXISTS idx_rag_tag ON rag_chunks(release_tag);
CREATE TABLE IF NOT EXISTS rag_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class StoredChunk:
    """検索用に読み出したチャンク。"""

    id: int
    path: str
    chunk_index: int
    text: str
    embedding: np.ndarray
    release_tag: str


def pack_embedding(vector: list[float] | np.ndarray) -> bytes:
    """float32 ベクトルを BLOB にする。"""
    arr = np.asarray(vector, dtype=np.float32)
    return arr.tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    """BLOB を float32 ベクトルにする。"""
    return np.frombuffer(blob, dtype=np.float32).copy()


class RagStore:
    """rag_chunks / rag_meta のラッパー。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("RagStore is not connected")
        return self._conn

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def get_meta(self, key: str, default: str = "") -> str:
        async with self.conn.execute(
            "SELECT value FROM rag_meta WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0]) if row else default

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO rag_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.conn.commit()

    async def clear_all(self) -> None:
        await self.conn.execute("DELETE FROM rag_chunks")
        await self.conn.commit()

    async def clear_except_prefixes(self, prefixes: list[str]) -> int:
        """指定接頭辞以外のチャンクを削除する（手動取り込み等を残す）。"""
        kept = [p for p in prefixes if p]
        if not kept:
            await self.clear_all()
            return 0
        clause = " AND ".join("path NOT LIKE ?" for _ in kept)
        params = tuple(f"{p}%" for p in kept)
        cursor = await self.conn.execute(
            f"DELETE FROM rag_chunks WHERE {clause}",
            params,
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)

    async def delete_path(self, path: str) -> None:
        await self.conn.execute("DELETE FROM rag_chunks WHERE path = ?", (path,))
        await self.conn.commit()

    async def delete_path_prefix(self, prefix: str) -> int:
        """パス接頭辞に一致するチャンクを削除し、件数を返す。"""
        cursor = await self.conn.execute(
            "DELETE FROM rag_chunks WHERE path LIKE ?",
            (f"{prefix}%",),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)

    async def replace_file_chunks(
        self,
        path: str,
        release_tag: str,
        content_hash: str,
        chunks: Iterable[tuple[int, str, list[float]]],
    ) -> int:
        """1 ファイル分のチャンクを置換する。戻り値は件数。"""
        await self.conn.execute("DELETE FROM rag_chunks WHERE path = ?", (path,))
        now = time.time()
        rows = [
            (
                path,
                index,
                text,
                pack_embedding(embedding),
                content_hash,
                release_tag,
                now,
            )
            for index, text, embedding in chunks
        ]
        if rows:
            await self.conn.executemany(
                """
                INSERT INTO rag_chunks
                    (path, chunk_index, text, embedding, content_hash,
                     release_tag, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        await self.conn.commit()
        return len(rows)

    async def chunk_count(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM rag_chunks") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def status(self) -> dict[str, str | int | float]:
        count = await self.chunk_count()
        current = await self.get_meta("current_tag")
        model = await self.get_meta("embed_model")
        indexed_at = await self.get_meta("indexed_at")
        help_ref = await self.get_meta("help_ref")
        return {
            "chunk_count": count,
            "current_tag": current,
            "embed_model": model,
            "indexed_at": float(indexed_at) if indexed_at else 0.0,
            "help_ref": help_ref or "",
        }

    async def load_all_embeddings(self) -> list[StoredChunk]:
        """全チャンクをメモリに載せる（中規模リポ向け）。"""
        async with self.conn.execute(
            """
            SELECT id, path, chunk_index, text, embedding, release_tag
            FROM rag_chunks
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            StoredChunk(
                id=int(r[0]),
                path=str(r[1]),
                chunk_index=int(r[2]),
                text=str(r[3]),
                embedding=unpack_embedding(r[4]),
                release_tag=str(r[5]),
            )
            for r in rows
        ]

    async def export_meta_json(self) -> str:
        """デバッグ用メタ JSON。"""
        status = await self.status()
        return json.dumps(status, ensure_ascii=False, indent=2)
