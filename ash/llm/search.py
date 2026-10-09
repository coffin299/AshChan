"""DuckDuckGo 検索（ddgs）と LLM 用ツール定義。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ddgs import DDGS

log = logging.getLogger(__name__)

# LLM に提示する web_search ツールの定義（OpenAI 互換 tools 形式）
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "DuckDuckGoでWeb検索する。最新情報・ニュース・固有名詞の確認・"
            "知らないことを調べるときに使う。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索クエリ（簡潔なキーワード）",
                }
            },
            "required": ["query"],
        },
    },
}


class SearchError(Exception):
    """検索に失敗したときの例外。"""


@dataclass
class SearchResult:
    """検索結果1件。"""

    title: str
    url: str
    snippet: str


def _search_sync(query: str, region: str, max_results: int) -> list[SearchResult]:
    """ddgs の同期 API で検索する（スレッドで実行される）。"""
    # テキスト検索を実行する
    raw = DDGS().text(query, region=region, max_results=max_results) or []
    # 必要なフィールドだけを取り出す
    return [
        SearchResult(
            title=str(item.get("title", "")),
            url=str(item.get("href", "")),
            snippet=str(item.get("body", "")),
        )
        for item in raw
        if item.get("href")
    ]


async def web_search(query: str, region: str, max_results: int) -> list[SearchResult]:
    """イベントループを止めないようスレッドで検索する。"""
    try:
        return await asyncio.to_thread(_search_sync, query, region, max_results)
    except Exception as exc:  # noqa: BLE001 - ddgs は多様な例外を投げるため
        # 失敗はローカルログに残し、呼び出し元へ SearchError で伝える
        log.warning("DuckDuckGo 検索に失敗しました (%s): %s", query, exc)
        raise SearchError(str(exc)) from exc


def format_results(results: list[SearchResult]) -> str:
    """検索結果を LLM に渡すテキストへ整形する。"""
    # 結果が無い場合はその旨を伝える
    if not results:
        return "検索結果は見つかりませんでした。"
    # 番号付きで タイトル / URL / 抜粋 を並べる
    lines = []
    for i, r in enumerate(results, start=1):
        lines.append(f"[{i}] {r.title}\nURL: {r.url}\n{r.snippet}")
    return "\n\n".join(lines)
