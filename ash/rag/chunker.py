"""テキスト / ソースコードを検索用チャンクへ分割する。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    """1チャンク分の本文とファイル内位置。"""

    path: str
    index: int
    text: str


def chunk_file(
    path: str,
    text: str,
    *,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
) -> list[TextChunk]:
    """ファイル内容をチャンク列に分割する。"""
    cleaned = text.replace("\r\n", "\n").strip()
    if not cleaned:
        return []
    lower = path.lower()
    if lower.endswith((".md", ".txt")):
        parts = _split_markdown(cleaned, chunk_size, chunk_overlap)
    elif lower.endswith(".cs"):
        parts = _split_csharp(cleaned, chunk_size, chunk_overlap)
    else:
        parts = _split_plain(cleaned, chunk_size, chunk_overlap)
    return [
        TextChunk(path=path, index=i, text=part)
        for i, part in enumerate(parts)
        if part.strip()
    ]


def _split_markdown(text: str, size: int, overlap: int) -> list[str]:
    """見出し単位でまとめてからサイズ調整する。"""
    sections = re.split(r"(?m)(?=^#{1,6}\s)", text)
    sections = [s.strip() for s in sections if s.strip()]
    if not sections:
        return _split_plain(text, size, overlap)
    merged: list[str] = []
    buf = ""
    for section in sections:
        if not buf:
            buf = section
        elif len(buf) + 2 + len(section) <= size:
            buf = f"{buf}\n\n{section}"
        else:
            merged.extend(_split_plain(buf, size, overlap))
            buf = section
    if buf:
        merged.extend(_split_plain(buf, size, overlap))
    return merged


_CS_BOUNDARY = re.compile(
    r"(?m)^(?=\s*(?:public|private|protected|internal|static|sealed|abstract|"
    r"partial|override|virtual|async|\[).*?"
    r"(?:class|struct|interface|enum|record|void|Task|bool|int|string|float|"
    r"double|var|IEnumerator)\b)"
)


def _split_csharp(text: str, size: int, overlap: int) -> list[str]:
    """クラス / メソッドっぽい境界で切ってからサイズ調整する。"""
    parts = _CS_BOUNDARY.split(text)
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) <= 1:
        return _split_plain(text, size, overlap)
    out: list[str] = []
    buf = ""
    for part in parts:
        if not buf:
            buf = part
        elif len(buf) + 2 + len(part) <= size:
            buf = f"{buf}\n\n{part}"
        else:
            out.extend(_split_plain(buf, size, overlap))
            buf = part
    if buf:
        out.extend(_split_plain(buf, size, overlap))
    return out


def _split_plain(text: str, size: int, overlap: int) -> list[str]:
    """固定長でオーバーラップ付きに切る。"""
    size = max(size, 200)
    overlap = max(0, min(overlap, size // 2))
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        # 可能な範囲で改行境界に寄せる
        if end < length:
            nl = text.rfind("\n", start + size // 2, end)
            if nl > start:
                end = nl + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks
