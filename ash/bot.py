"""Bot 本体: 設定・DB・通知・LLM を束ね、Cog をロードする。"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ash.config import BASE_DIR, Config, load_config
from ash.db import Database
from ash.llm.client import LLMClient
from ash.notifier import COLOR_DANGER, COLOR_INFO, Notifier, make_embed
from ash.presence import PresenceManager

log = logging.getLogger(__name__)

# ロードする Cog のモジュール一覧
COG_EXTENSIONS = (
    "ash.cogs.honeypot",
    "ash.cogs.moderation",
    "ash.cogs.antinuke",
    "ash.cogs.fixlink",
    "ash.cogs.autorole",
    "ash.cogs.welcome",
    "ash.cogs.github",
    "ash.cogs.support",
    "ash.cogs.chat",
    "ash.cogs.admin",
)
# SQLite ファイルの保存先
DB_PATH = BASE_DIR / "data" / "ashchan.db"


def build_intents() -> discord.Intents:
    """必要な Gateway Intents を組み立てる。"""
    # 既定（guilds / moderation / auto_moderation 等）を土台にする
    intents = discord.Intents.default()
    # メッセージ本文の読み取り（Privileged）
    intents.message_content = True
    # メンバー情報の取得（Privileged）
    intents.members = True
    return intents


class AshBot(commands.Bot):
    """AshChan の Bot クラス。"""

    def __init__(self, config: Config) -> None:
        # プレフィックスコマンドは使わないのでメンションのみにする
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # 設定・依存オブジェクトを保持する
        self.config = config
        self.db = Database(DB_PATH)
        self.notifier = Notifier(self)
        self.llm = LLMClient(config.llm)
        self.presence = PresenceManager(self)
        # システムプロンプトを読み込む
        self.system_prompt = self._read_prompt()
        # 起動時刻（/ash status 用）
        self.started_at = time.time()
        # on_ready の多重通知防止フラグ
        self._ready_notified = False

    def _read_prompt(self) -> str:
        """prompt_file からシステムプロンプトを読み込む。"""
        # config.yaml からの相対パスで解決する
        path = self.config.resolve_path(self.config.llm.prompt_file)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            # 読めなくても起動は継続し、警告だけ出す
            log.warning("プロンプトファイルを読めませんでした (%s): %s", path, exc)
            return ""

    async def reload_config(self) -> None:
        """config.yaml とプロンプトを再読込する。"""
        # 設定を読み直す（失敗時は例外を呼び出し元へ）
        self.config = load_config(self.config.path)
        # 古い LLM クライアントを閉じ、新しい設定で作り直す
        await self.llm.close()
        self.llm = LLMClient(self.config.llm)
        # プロンプトを読み直す
        self.system_prompt = self._read_prompt()
        # プレゼンス設定が変わったらすぐ反映する
        await self.presence.bump()

    async def setup_hook(self) -> None:
        """ログイン後・接続前の初期化処理。"""
        # DB に接続する
        await self.db.connect()
        # Cog を順にロードする
        for ext in COG_EXTENSIONS:
            await self.load_extension(ext)
        # スラッシュコマンドのエラーハンドラを登録する
        self.tree.on_error = self._on_app_command_error
        # スラッシュコマンドを同期する
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """スラッシュコマンドを Discord に同期する。"""
        # guild_ids 指定があればギルド単位で即時同期する
        if self.config.discord.guild_ids:
            for guild_id in self.config.discord.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            return
        # 指定が無ければグローバル同期する
        await self.tree.sync()

    async def on_ready(self) -> None:
        """接続完了時の処理。"""
        # 再接続のたびに通知しないよう初回のみ
        if self._ready_notified:
            return
        self._ready_notified = True
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "-"))
        # カスタムステータスのローテーションを開始する
        self.presence.start()
        await self.presence.bump()
        # 起動を dev 通知する
        await self.notifier.notify_dev(
            make_embed(
                "アッシュ起動",
                f"{self.user} としてログインしました。",
                COLOR_INFO,
                {
                    "サーバー数": str(len(self.guilds)),
                    "モデル": " → ".join(self.config.llm.model_chain),
                },
            )
        )

    async def close(self) -> None:
        """終了処理: 停止通知 → Discord 切断 → ローカル資源解放。"""
        # バックグラウンドのプレゼンス更新を先に止める（切断後の WS 触りを防ぐ）
        await self.presence.stop(clear_activity=False)
        # まだ接続中なら停止を通知する
        if self.is_ready() and not self.is_closed():
            try:
                await self.notifier.notify_dev(
                    make_embed("アッシュ停止", "ボットを停止します。", COLOR_INFO)
                )
            except Exception:
                log.exception("停止通知に失敗しました")
        # Discord を先に閉じ、keep-alive を正規に止める
        await super().close()
        # 切断後にローカル資源を解放する
        try:
            await self.llm.close()
        except Exception:
            log.exception("LLM クライアントのクローズに失敗しました")
        try:
            await self.db.close()
        except Exception:
            log.exception("DB のクローズに失敗しました")

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """イベント処理中の未捕捉例外を記録・通知する。"""
        # 例外のトレースバックを取得する
        tb = traceback.format_exc()
        log.error("Unhandled exception in %s\n%s", event_method, tb)
        # dev 通知チャンネルへ要約を送る
        await self.notifier.notify_dev(
            make_embed(
                "未捕捉の例外",
                f"```py\n{tb[-3500:]}\n```",
                COLOR_DANGER,
                {"イベント": event_method},
            )
        )

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """スラッシュコマンドの例外を記録・通知する。"""
        # 権限不足は利用者に伝えるだけ
        if isinstance(error, app_commands.MissingPermissions):
            message = "このコマンドを使う権限がないみたい。"
        else:
            # それ以外は記録して dev 通知する
            tb = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            log.error("App command error\n%s", tb)
            command = interaction.command
            await self.notifier.notify_dev(
                make_embed(
                    "コマンドエラー",
                    f"```py\n{tb[-3500:]}\n```",
                    COLOR_DANGER,
                    {"コマンド": command.qualified_name if command else "-"},
                )
            )
            message = "ごめん、コマンドの処理でエラーが出ちゃった。"
        # 応答済みかどうかで送信方法を切り替える
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def excepthook_to_log() -> None:
    """同期処理の未捕捉例外もログに残す。"""
    # 既定のフックをログ出力に置き換える
    def _hook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
