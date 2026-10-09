"""エントリポイント: `python -B -m ash` で起動する。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

# バイトコード（__pycache__）を書き出さない
sys.dont_write_bytecode = True

from ash.bot import AshBot, excepthook_to_log  # noqa: E402
from ash.config import BASE_DIR, ConfigError, load_config  # noqa: E402

# ログファイルの保存先
LOG_DIR = BASE_DIR / "logs"


def setup_logging() -> None:
    """コンソールとローテーションファイルへのログ出力を設定する。"""
    # ログディレクトリを用意する
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 出力フォーマットを統一する
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    # コンソール出力
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    # 1MB × 5世代でローテーションするファイル出力
    file_handler = RotatingFileHandler(
        LOG_DIR / "ashchan.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    # ルートロガーへ登録する
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)
    # discord.py の詳細な HTTP ログは抑える
    logging.getLogger("discord.http").setLevel(logging.WARNING)


def main() -> int:
    """設定を読み込みボットを起動する。"""
    # ログ設定と例外フックを有効にする
    setup_logging()
    excepthook_to_log()
    log = logging.getLogger("ash")
    try:
        # config.yaml を読み込む
        config = load_config()
    except ConfigError as exc:
        # 設定不備はメッセージを出して終了する
        log.error("%s", exc)
        return 1
    # ボットを生成して起動する（ログ設定は済んでいるので discord.py には任せない）
    bot = AshBot(config)
    bot.run(config.discord.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
