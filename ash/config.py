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
ENV_GITHUB_TOKEN = "GITHUB_TOKEN"
ENV_GH_TOKEN = "GH_TOKEN"

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
    # 優先モデルのあとに試すフォールバック（model 自身は含めなくてよい）
    models: list[str] = field(default_factory=list)
    prompt_file: str = "prompt.md"
    temperature: float = 0.8
    max_tokens: int = 800
    history_turns: int = 10
    user_cooldown_sec: float = 5.0
    stream_edit_interval_sec: float = 2.0
    timeout_sec: float = 60.0
    fallback_replies: list[str] = field(default_factory=list)

    @property
    def model_chain(self) -> list[str]:
        """優先モデル → フォールバックの重複なしリスト。"""
        chain: list[str] = []
        for name in (self.model, *self.models):
            text = str(name).strip()
            if text and text not in chain:
                chain.append(text)
        return chain or ["meta/llama-3.3-70b-instruct"]


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
class SupportConfig:
    """VRCast サポートフォーラム / RAG 設定。"""

    enabled: bool = False
    forum_channel_ids: list[int] = field(default_factory=list)
    admin_user_ids: list[int] = field(default_factory=list)
    resolved_tag_name: str = "解決済み"
    repo: str = "coffin299/VRCast"
    local_repo_path: str = ""
    # RAG の GitHub API 用（remote bootstrap / sync）。Webhook secret とは別
    github_token: str = ""
    # ヘルプサイト用ブランチ（空なら取り込まない）。例: webpage → github.io/VRCast/help/
    help_ref: str = "webpage"
    help_include_globs: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    embedding_model: str = "nvidia/nemotron-3-embed-1b"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    top_k: int = 8
    chunk_size: int = 1200
    chunk_overlap: int = 150
    embed_batch_size: int = 8
    embed_rpm: int = 40
    max_file_chars: int = 200_000
    max_attachment_chars: int = 20_000
    prompt_file: str = "prompt_support.md"
    max_turns_before_hint: int = 6
    footer_hint: str = (
        "アッシュちゃんはコードベースから解決策を考えます。"
        "リプライで会話を続けるか、下記ボタンから開発者にエスカレーションしてください。"
    )
    llm_retry_count: int = 3
    llm_retry_delay_sec: float = 30.0
    resolve_button_label: str = "解決した"
    escalate_button_label: str = "開発者に聞く"


@dataclass
class PresenceConfig:
    """Discord カスタムステータス（いま何してるか）設定。"""

    enabled: bool = True
    rotate_sec: float = 20.0
    idle_statuses: list[str] = field(default_factory=list)
    support_template: str = "サポート中/{forum}"
    chat_busy: str = "会話中"
    rag_rebuild: str = "RAG再構築中"
    rag_sync: str = "RAG更新中"


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
    support: SupportConfig
    presence: PresenceConfig
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
    support_cfg = _build(SupportConfig, raw.get("support"))
    presence_cfg = _build(PresenceConfig, raw.get("presence"))

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
    # サポート / RAG の ID・グロブを正規化する
    support_cfg.forum_channel_ids = _int_list(support_cfg.forum_channel_ids)
    support_cfg.admin_user_ids = _int_list(support_cfg.admin_user_ids)
    if not support_cfg.include_globs:
        support_cfg.include_globs = [
            "**/*.cs",
            "**/*.md",
            "**/*.txt",
            "**/*.json",
            "**/*.xml",
            "**/*.uxml",
            "**/*.uss",
            "**/*.shader",
            "**/*.hlsl",
            "**/*.compute",
            "**/*.asmdef",
        ]
    else:
        support_cfg.include_globs = [str(g) for g in support_cfg.include_globs]
    if not support_cfg.exclude_globs:
        support_cfg.exclude_globs = [
            "**/Library/**",
            "**/obj/**",
            "**/bin/**",
            "**/.git/**",
            "**/docs/images/**",
            "**/*.meta",
        ]
    else:
        support_cfg.exclude_globs = [str(g) for g in support_cfg.exclude_globs]
    if not support_cfg.help_include_globs:
        support_cfg.help_include_globs = [
            "help/**/*.html",
            "ja/help/**/*.html",
            "llms.txt",
        ]
    else:
        support_cfg.help_include_globs = [
            str(g) for g in support_cfg.help_include_globs if str(g).strip()
        ]
    support_cfg.help_ref = str(support_cfg.help_ref or "").strip()

    # LLM フォールバック一覧を正規化する（単一文字列でもリスト化）
    raw_models = llm_cfg.models
    if isinstance(raw_models, str):
        raw_models = [raw_models]
    elif not isinstance(raw_models, list):
        raw_models = []
    llm_cfg.models = [str(m).strip() for m in raw_models if str(m).strip()]
    # model が空なら models の先頭を優先モデルにする
    if not str(llm_cfg.model or "").strip() and llm_cfg.models:
        llm_cfg.model = llm_cfg.models[0]
    llm_cfg.model = str(llm_cfg.model or "").strip() or "meta/llama-3.3-70b-instruct"

    # 環境変数があれば秘密情報を上書きする
    discord_cfg.token = os.environ.get(ENV_DISCORD_TOKEN) or discord_cfg.token
    llm_cfg.api_key = os.environ.get(ENV_NVIDIA_API_KEY) or llm_cfg.api_key
    github_cfg.secret = os.environ.get(ENV_GITHUB_SECRET) or github_cfg.secret
    # RAG 用 GitHub トークン（環境変数が最優先）
    support_cfg.github_token = (
        os.environ.get(ENV_GITHUB_TOKEN)
        or os.environ.get(ENV_GH_TOKEN)
        or support_cfg.github_token
        or ""
    )
    support_cfg.embedding_api_key = (
        os.environ.get("RAG_API_KEY")
        or support_cfg.embedding_api_key
        or llm_cfg.api_key
    )
    if os.environ.get("RAG_BASE_URL"):
        support_cfg.embedding_base_url = os.environ["RAG_BASE_URL"]
    if os.environ.get("RAG_EMBED_MODEL"):
        support_cfg.embedding_model = os.environ["RAG_EMBED_MODEL"]
    if not support_cfg.embedding_base_url:
        support_cfg.embedding_base_url = llm_cfg.base_url

    # プレゼンスの待機文言（空なら既定セット）
    if not presence_cfg.idle_statuses:
        presence_cfg.idle_statuses = [
            "通常待機",
            "暇だよ",
            "VRCastのdocs眺めてる",
            "ねむい…",
        ]
    else:
        presence_cfg.idle_statuses = [
            str(s).strip() for s in presence_cfg.idle_statuses if str(s).strip()
        ]

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
        support=support_cfg,
        presence=presence_cfg,
        path=config_path,
    )
