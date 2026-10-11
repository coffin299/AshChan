"""config.yaml の読み込みと型付き設定オブジェクトへの変換。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import yaml

# プロジェクトのルートディレクトリ（ash パッケージの1つ上）
BASE_DIR = Path(__file__).resolve().parent.parent
# 既定の設定ファイルパス
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"
# 秘密情報を上書きする環境変数名
ENV_DISCORD_TOKEN = "ASH_DISCORD_TOKEN"
ENV_NVIDIA_API_KEY = "NVIDIA_API_KEY"
ENV_GITHUB_SECRET = "GITHUB_WEBHOOK_SECRET"

T = TypeVar("T")


class ConfigError(Exception):
    """設定内容が不正なときに送出する例外。"""


@dataclass
class DiscordConfig:
    """Discord 接続設定。"""

    token: str = ""
    guild_ids: list[int] = field(default_factory=list)


@dataclass
class NotifyConfig:
    """通知先設定。"""

    dev_channel_id: int = 0
    emergency_dm_user_ids: list[int] = field(default_factory=list)


@dataclass
class LLMConfig:
    """NVIDIA NIM による会話設定。"""

    api_key: str = ""
    base_url: str = "https://integrate.api.nvidia.com/v1"
    model: str = "meta/llama-3.3-70b-instruct"
    prompt_file: str = "prompt.md"
    temperature: float = 0.8
    max_tokens: int = 800
    history_turns: int = 10
    user_cooldown_sec: float = 5.0
    stream_edit_interval_sec: float = 2.0
    timeout_sec: float = 60.0
    fallback_replies: list[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    """DuckDuckGo 検索設定。"""

    enabled: bool = True
    max_results: int = 5
    max_calls_per_reply: int = 3
    region: str = "jp-jp"
    indicator: str = "検索中...🔍 {query}"
    show_sources: bool = True


@dataclass
class AutomodConfig:
    """Discord AutoMod 連携設定。"""

    enabled: bool = True
    keyword_rule: bool = True
    mention_spam_rule: bool = True
    spam_rule: bool = True


@dataclass
class ModerationConfig:
    """ルールベースモデレーション設定。"""

    enabled: bool = True
    exempt_role_ids: list[int] = field(default_factory=list)
    timeout_minutes: int = 10
    mention_limit: int = 5
    block_invites: bool = True
    invite_patterns: list[str] = field(default_factory=list)
    block_suspicious_links: bool = True
    suspicious_link_patterns: list[str] = field(default_factory=list)
    ng_words: list[str] = field(default_factory=list)
    automod: AutomodConfig = field(default_factory=AutomodConfig)


@dataclass
class AntiNukeConfig:
    """アンチnuke 設定。"""

    enabled: bool = True
    window_seconds: int = 30
    alert_cooldown_seconds: int = 300
    thresholds: dict[str, int] = field(default_factory=dict)
    whitelist_user_ids: list[int] = field(default_factory=list)


@dataclass
class HoneypotConfig:
    """ハニーポット設定。"""

    enabled: bool = True
    channel_ids: list[int] = field(default_factory=list)
    exempt_role_ids: list[int] = field(default_factory=list)
    max_kicks: int = 3
    purge_scan_days: int = 14


@dataclass
class AutoRoleConfig:
    """新規参加者へのロール自動付与設定。"""

    enabled: bool = False
    role_ids: list[int] = field(default_factory=list)
    bot_role_ids: list[int] = field(default_factory=list)
    wait_for_screening: bool = True


@dataclass
class WelcomeConfig:
    """新規参加者へのようこそメッセージ設定。"""

    enabled: bool = False
    channel_id: int = 0
    wait_for_screening: bool = True
    max_lines: int = 3
    extra_instructions: str = ""
    max_llm_per_minute: int = 5
    fallback_messages: list[str] = field(default_factory=list)


@dataclass
class IssueRoute:
    """特定の Issue / PR 番号の通知先。"""

    numbers: list[int]
    channel_id: int
    repo: str = ""


@dataclass
class GitHubConfig:
    """GitHub Webhook 受信（Issue / リリース / コミット通知）設定。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/github/webhook"
    secret: str = ""
    default_channel_id: int = 0
    issue_channel_id: int = 0
    issue_routes: list[IssueRoute] = field(default_factory=list)
    release_channel_id: int = 0
    commit_channel_id: int = 0
    commit_branches: list[str] = field(default_factory=list)
    events: dict[str, bool] = field(default_factory=dict)
    ignore_bots: bool = True
    max_body_chars: int = 1000
    max_release_body_chars: int = 2000
    max_commits: int = 10


@dataclass
class FixLinkRule:
    """FixLink の置換ルール1件。"""

    pattern: str
    replace: str
    path_pattern: str = ""


@dataclass
class FixLinkConfig:
    """FixLink 設定。"""

    enabled: bool = True
    suppress_original_embed: bool = True
    ignore_channel_ids: list[int] = field(default_factory=list)
    rules: list[FixLinkRule] = field(default_factory=list)


@dataclass
class Config:
    """ボット全体の設定。"""

    discord: DiscordConfig
    notify: NotifyConfig
    llm: LLMConfig
    search: SearchConfig
    moderation: ModerationConfig
    antinuke: AntiNukeConfig
    honeypot: HoneypotConfig
    fixlink: FixLinkConfig
    autorole: AutoRoleConfig
    welcome: WelcomeConfig
    github: GitHubConfig
    path: Path

    def resolve_path(self, relative: str) -> Path:
        """設定ファイルのあるディレクトリを基準にパスを解決する。"""
        # 絶対パスならそのまま、相対パスなら config.yaml の場所を基準にする
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.path.parent / candidate


def _build(cls: type[T], data: Any) -> T:
    """辞書から dataclass を生成する（未知のキーは無視し、欠損は既定値）。"""
    # セクションが未記載・不正型なら空辞書として扱う
    if not isinstance(data, dict):
        data = {}
    # dataclass に定義されたフィールド名だけを拾う
    known = {f.name for f in fields(cls)}
    # 定義済みかつ None でない値だけを渡し、残りは既定値に任せる
    kwargs = {k: v for k, v in data.items() if k in known and v is not None}
    return cls(**kwargs)


def _int_list(values: Any) -> list[int]:
    """ID リストを整数リストへ正規化する。"""
    # 未設定や単一値でも扱えるようにリスト化する
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    # 文字列で書かれた ID も整数化し、0 や空は除外する
    return [int(v) for v in values if str(v).strip() not in ("", "0")]


def load_config(path: Path | None = None) -> Config:
    """config.yaml を読み込み Config を返す。"""
    # パス未指定なら既定の config.yaml を使う
    config_path = path or DEFAULT_CONFIG_PATH
    # ファイルが無い場合は start.bat の実行を促す
    if not config_path.exists():
        raise ConfigError(
            f"{config_path} が見つかりません。start.bat を実行して生成してください。"
        )
    # UTF-8 で YAML を読み込む
    with config_path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    # 各セクションを dataclass に変換する
    discord_cfg = _build(DiscordConfig, raw.get("discord"))
    notify_cfg = _build(NotifyConfig, raw.get("notify"))
    llm_cfg = _build(LLMConfig, raw.get("llm"))
    search_cfg = _build(SearchConfig, raw.get("search"))
    moderation_raw = raw.get("moderation") or {}
    moderation_cfg = _build(ModerationConfig, moderation_raw)
    antinuke_cfg = _build(AntiNukeConfig, raw.get("antinuke"))
    honeypot_cfg = _build(HoneypotConfig, raw.get("honeypot"))
    fixlink_raw = raw.get("fixlink") or {}
    fixlink_cfg = _build(FixLinkConfig, fixlink_raw)
    autorole_cfg = _build(AutoRoleConfig, raw.get("autorole"))
    welcome_cfg = _build(WelcomeConfig, raw.get("welcome"))
    github_raw = raw.get("github") or {}
    github_cfg = _build(GitHubConfig, github_raw)

    # ネストした AutoMod 設定を dataclass 化する
    moderation_cfg.automod = _build(AutomodConfig, moderation_raw.get("automod"))
    # FixLink のルールを dataclass 化する（pattern / replace が無いものは捨てる）
    fixlink_cfg.rules = [
        FixLinkRule(
            pattern=str(rule["pattern"]),
            replace=str(rule["replace"]),
            path_pattern=str(rule.get("path_pattern") or ""),
        )
        for rule in (fixlink_raw.get("rules") or [])
        if isinstance(rule, dict) and rule.get("pattern") and rule.get("replace")
    ]

    # ID 系のリスト・値を整数に正規化する
    discord_cfg.guild_ids = _int_list(discord_cfg.guild_ids)
    notify_cfg.dev_channel_id = int(notify_cfg.dev_channel_id or 0)
    notify_cfg.emergency_dm_user_ids = _int_list(notify_cfg.emergency_dm_user_ids)
    moderation_cfg.exempt_role_ids = _int_list(moderation_cfg.exempt_role_ids)
    antinuke_cfg.whitelist_user_ids = _int_list(antinuke_cfg.whitelist_user_ids)
    honeypot_cfg.channel_ids = _int_list(honeypot_cfg.channel_ids)
    honeypot_cfg.exempt_role_ids = _int_list(honeypot_cfg.exempt_role_ids)
    fixlink_cfg.ignore_channel_ids = _int_list(fixlink_cfg.ignore_channel_ids)
    autorole_cfg.role_ids = _int_list(autorole_cfg.role_ids)
    autorole_cfg.bot_role_ids = _int_list(autorole_cfg.bot_role_ids)
    welcome_cfg.channel_id = int(welcome_cfg.channel_id or 0)
    github_cfg.default_channel_id = int(github_cfg.default_channel_id or 0)
    github_cfg.issue_channel_id = int(github_cfg.issue_channel_id or 0)
    github_cfg.release_channel_id = int(github_cfg.release_channel_id or 0)
    github_cfg.commit_channel_id = int(github_cfg.commit_channel_id or 0)
    # ブランチ名の一覧を文字列リストへ正規化する（単一値・空要素も許容）
    branches = github_cfg.commit_branches
    if not isinstance(branches, list):
        branches = [branches]
    github_cfg.commit_branches = [
        str(b).strip() for b in branches if str(b).strip()
    ]
    # 番号別ルートを dataclass 化する（番号・チャンネルが無いものは捨てる）
    github_cfg.issue_routes = [
        IssueRoute(
            numbers=_int_list(route.get("numbers")),
            channel_id=int(route["channel_id"]),
            # リポジトリ名は大文字小文字を区別せず照合できるよう小文字化する
            repo=str(route.get("repo") or "").lower(),
        )
        for route in (github_raw.get("issue_routes") or [])
        if isinstance(route, dict)
        and route.get("numbers")
        and route.get("channel_id")
    ]
    github_cfg.events = {
        str(k): bool(v) for k, v in (github_cfg.events or {}).items()
    }
    # 閾値は整数辞書に正規化する
    antinuke_cfg.thresholds = {
        str(k): int(v) for k, v in (antinuke_cfg.thresholds or {}).items()
    }

    # 環境変数があれば秘密情報を上書きする
    discord_cfg.token = os.environ.get(ENV_DISCORD_TOKEN) or discord_cfg.token
    llm_cfg.api_key = os.environ.get(ENV_NVIDIA_API_KEY) or llm_cfg.api_key
    github_cfg.secret = os.environ.get(ENV_GITHUB_SECRET) or github_cfg.secret

    # Discord トークンは必須
    if not discord_cfg.token:
        raise ConfigError(
            "discord.token が未設定です。config.yaml を編集してください。"
        )

    return Config(
        discord=discord_cfg,
        notify=notify_cfg,
        llm=llm_cfg,
        search=search_cfg,
        moderation=moderation_cfg,
        antinuke=antinuke_cfg,
        honeypot=honeypot_cfg,
        fixlink=fixlink_cfg,
        autorole=autorole_cfg,
        welcome=welcome_cfg,
        github=github_cfg,
        path=config_path,
    )
