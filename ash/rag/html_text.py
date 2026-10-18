"""HTML から検索用のプレーンテキストを取り出す。"""

from __future__ import annotations

from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    """script/style を除いたテキストノードを集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def html_to_text(html: str) -> str:
    """HTML をチャンク用のテキストに変換する。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # 壊れた HTML でも最低限タグ除去
        import re

        return re.sub(r"<[^>]+>", " ", html)
    raw = " ".join(parser.parts)
    # 連続空白・改行を整える
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line).strip()
