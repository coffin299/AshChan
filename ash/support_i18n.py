"""サポートフォーラムの日英 UI / 言語判定。"""

from __future__ import annotations

import re
from typing import Literal

SupportLocale = Literal["ja", "en"]

# ひらがな・カタカナ・漢字があれば日本語寄りとみなす
_JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_locale(text: str) -> SupportLocale:
    """初回投稿テキストからスレ言語を推定する。

    日本語文字（かな漢字）があれば ja。無ければラテン文字多めで en。
    """
    sample = (text or "").strip()
    if not sample:
        return "ja"
    ja = len(_JA_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    if ja > 0:
        return "ja"
    if latin >= 3:
        return "en"
    return "ja"


PHASE_TEXT_I18N: dict[SupportLocale, dict[str, str]] = {
    "ja": {
        "start": "ちょっと見てみるね…",
        "attachments": "ログ／出力ファイル、確認してる…",
        "rag": "ドキュメントとコードから手がかり探してる…",
        "llm": "方針まとめてる…",
        "retry": "うまくいかなかった…30秒あとにもう一回やってみる",
    },
    "en": {
        "start": "Taking a look…",
        "attachments": "Checking logs / output files…",
        "rag": "Searching docs and code for clues…",
        "llm": "Putting a plan together…",
        "retry": "That didn’t work… retrying in 30 seconds",
    },
}

UI: dict[SupportLocale, dict[str, str]] = {
    "ja": {
        "footer_hint": (
            "アッシュちゃんはコードベースから解決策を考えます。"
            "リプライで会話を続けるか、下記ボタンから開発者にエスカレーションしてください。"
        ),
        "resolve_button": "解決した",
        "escalate_button": "開発者に聞く",
        "failure_body": (
            "ごめん、いま LLM サポートがうまく動かなくて失敗しちゃった。"
            "開発者にバトン渡すね。しばらく待ってて。"
        ),
        "failure_mention": "LLM サポートに失敗したよ。手動で見てほしい。",
        "resolve_done": "解決として閉じたよ。お疲れ！",
        "resolve_closed_body": "解決済みとしてクローズしたよ。また何かあったらスレ立ててね。",
        "resolve_forbidden": "スレ主かスタッフだけが解決にできるよ。",
        "escalate_busy": "いまエスカレーション処理中だよ。",
        "escalate_already": "このスレはもうエスカレーション済みだよ。",
        "escalate_generating": "サマリー生成中…",
        "escalate_failed_progress": "サマリー生成に失敗したよ。ログを確認してね。",
        "escalate_failed_followup": "まとめの生成に失敗したよ",
        "escalate_done_followup": "開発者向けのまとめを出してメンションしたよ。",
        "escalate_done_body": "開発者にエスカレーション済み。詳細は上のまとめを見てね。",
        "escalate_header": "開発者エスカレーション",
        "button_busy": "いま処理中か、もう使えないボタンだよ。",
        "empty_body": "（本文なし）",
        "prev_reply": "（前回の返信）",
    },
    "en": {
        "footer_hint": (
            "Ash looks through the codebase for fixes. "
            "Reply to continue, or use the buttons below to escalate to a developer."
        ),
        "resolve_button": "Resolved",
        "escalate_button": "Ask a developer",
        "failure_body": (
            "Sorry — LLM support failed just now. "
            "I’m handing this off to a developer. Please hang tight."
        ),
        "failure_mention": "LLM support failed. Please take a look manually.",
        "resolve_done": "Marked as resolved and closed. Thanks!",
        "resolve_closed_body": (
            "Closed as resolved. Open a new thread if you need help again."
        ),
        "resolve_forbidden": "Only the thread owner or staff can mark this resolved.",
        "escalate_busy": "Escalation is already in progress.",
        "escalate_already": "This thread was already escalated.",
        "escalate_generating": "Generating developer summary…",
        "escalate_failed_progress": (
            "Failed to generate the summary. Please check the logs."
        ),
        "escalate_failed_followup": "Failed to generate the summary",
        "escalate_done_followup": "Posted the developer summary and mentioned the team.",
        "escalate_done_body": (
            "Escalated to a developer. See the summary above for details."
        ),
        "escalate_header": "Developer escalation",
        "button_busy": "This button is busy or no longer available.",
        "empty_body": "(no text)",
        "prev_reply": "(previous reply)",
    },
}


def t(locale: str, key: str) -> str:
    """ロケール付き UI 文字列。未知なら ja。"""
    loc: SupportLocale = "en" if locale == "en" else "ja"
    return UI[loc].get(key) or UI["ja"].get(key) or key


def phase_text(locale: str, phase: str) -> str:
    loc: SupportLocale = "en" if locale == "en" else "ja"
    table = PHASE_TEXT_I18N[loc]
    return table.get(phase) or PHASE_TEXT_I18N["ja"].get(phase) or phase


def normalize_locale(value: str | None) -> SupportLocale:
    return "en" if (value or "").strip().lower() == "en" else "ja"
