"""SQLite による永続化（ハニーポット回数・投稿インデックス・会話履歴）。"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

# テーブル定義（起動時に存在しなければ作成する）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS honeypot_strikes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS message_index (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_user
    ON message_index (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_message_created
    ON message_index (created_at);
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_channel
    ON chat_history (channel_id, id);
CREATE TABLE IF NOT EXISTS support_threads (
    thread_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open',
    owner_id INTEGER NOT NULL DEFAULT 0,
    last_action_message_id INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class Database:
    """ボットが使う SQLite データベースのラッパー。"""

    def __init__(self, path: Path) -> None:
        # DB ファイルのパスを保持する
        self._path = path
        # 接続は connect() で確立する
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """確立済みの接続を返す。"""
        # 未接続での利用はプログラムミスなので例外にする
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        """DB に接続し、スキーマを作成する。"""
        # data ディレクトリが無ければ作る
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 接続してスキーマを適用する
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        """DB 接続を閉じる。"""
        # 接続済みのときだけ閉じる
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------
    # ハニーポット回数
    # ------------------------------------------------------------
    async def increment_strike(self, guild_id: int, user_id: int) -> int:
        """ハニーポット回数を1増やし、更新後の回数を返す。"""
        # 行が無ければ1で作成、あれば加算する
        await self.conn.execute(
            """
            INSERT INTO honeypot_strikes (guild_id, user_id, count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
            """,
            (guild_id, user_id, time.time()),
        )
        await self.conn.commit()
        # 更新後の値を読み直して返す
        return await self.get_strike(guild_id, user_id)

    async def get_strike(self, guild_id: int, user_id: int) -> int:
        """現在のハニーポット回数を返す（記録が無ければ0）。"""
        # 該当ユーザーの回数を取得する
        async with self.conn.execute(
            "SELECT count FROM honeypot_strikes WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        # 記録が無ければ0回
        return int(row[0]) if row else 0

    async def reset_strike(self, guild_id: int, user_id: int) -> None:
        """ハニーポット回数を消去する。"""
        # 行ごと削除して0回に戻す
        await self.conn.execute(
            "DELETE FROM honeypot_strikes WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------
    # 投稿インデックス（ハニーポットの全投稿削除用）
    # ------------------------------------------------------------
    async def index_message(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        created_at: float,
    ) -> None:
        """投稿をインデックスに登録する。"""
        # 同じメッセージの重複登録は無視する
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO message_index
                (message_id, guild_id, channel_id, user_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, guild_id, channel_id, user_id, created_at),
        )
        await self.conn.commit()

    async def get_user_messages(
        self, guild_id: int, user_id: int
    ) -> list[tuple[int, int, float]]:
        """ユーザーの投稿一覧を (channel_id, message_id, created_at) で返す。"""
        # 該当ユーザーのインデックスを全件取得する
        async with self.conn.execute(
            """
            SELECT channel_id, message_id, created_at FROM message_index
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(r[0]), int(r[1]), float(r[2])) for r in rows]

    async def forget_user_messages(self, guild_id: int, user_id: int) -> None:
        """ユーザーの投稿インデックスを消去する。"""
        # 削除処理後にインデックスも掃除する
        await self.conn.execute(
            "DELETE FROM message_index WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def forget_message(self, message_id: int) -> None:
        """単一メッセージのインデックスを消去する。"""
        # メッセージ削除イベントに追従する
        await self.conn.execute(
            "DELETE FROM message_index WHERE message_id = ?", (message_id,)
        )
        await self.conn.commit()

    async def prune_messages(self, older_than: float) -> int:
        """指定時刻より古いインデックスを削除し、件数を返す。"""
        # 保持期間を過ぎた行を消す
        cursor = await self.conn.execute(
            "DELETE FROM message_index WHERE created_at < ?", (older_than,)
        )
        await self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------
    # 会話履歴
    # ------------------------------------------------------------
    async def add_history(self, channel_id: int, role: str, content: str) -> None:
        """会話履歴に1件追加する。"""
        # role は "user" または "assistant"
        await self.conn.execute(
            """
            INSERT INTO chat_history (channel_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (channel_id, role, content, time.time()),
        )
        await self.conn.commit()

    async def get_history(self, channel_id: int, limit: int) -> list[dict[str, str]]:
        """直近 limit 件の履歴を古い順で返す。"""
        # 0件指定なら DB を引かない
        if limit <= 0:
            return []
        # 新しい順に取って後で反転する
        async with self.conn.execute(
            """
            SELECT role, content FROM chat_history
            WHERE channel_id = ? ORDER BY id DESC LIMIT ?
            """,
            (channel_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        # LLM に渡す順（古い→新しい）に並べ替える
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    async def trim_history(self, channel_id: int, keep: int) -> None:
        """チャンネルの履歴を直近 keep 件だけ残して削除する。"""
        # 直近 keep 件より古い行を消す
        await self.conn.execute(
            """
            DELETE FROM chat_history
            WHERE channel_id = ? AND id NOT IN (
                SELECT id FROM chat_history
                WHERE channel_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (channel_id, channel_id, max(keep, 0)),
        )
        await self.conn.commit()

    # ------------------------------------------------------------
    # サポートフォーラムスレ状態
    # ------------------------------------------------------------
    async def ensure_support_thread(
        self, thread_id: int, owner_id: int
    ) -> dict[str, int | str]:
        """スレ行が無ければ open で作成し、現在の状態を返す。"""
        now = time.time()
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO support_threads
                (thread_id, status, owner_id, last_action_message_id,
                 turn_count, updated_at)
            VALUES (?, 'open', ?, 0, 0, ?)
            """,
            (thread_id, owner_id, now),
        )
        await self.conn.commit()
        return await self.get_support_thread(thread_id)

    async def get_support_thread(
        self, thread_id: int
    ) -> dict[str, int | str]:
        """サポートスレ状態を返す（無ければ空 dict）。"""
        async with self.conn.execute(
            """
            SELECT status, owner_id, last_action_message_id, turn_count
            FROM support_threads WHERE thread_id = ?
            """,
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {}
        return {
            "status": str(row[0]),
            "owner_id": int(row[1]),
            "last_action_message_id": int(row[2]),
            "turn_count": int(row[3]),
        }

    async def set_support_status(self, thread_id: int, status: str) -> None:
        """スレ状態を更新する。"""
        await self.conn.execute(
            """
            UPDATE support_threads
            SET status = ?, updated_at = ?
            WHERE thread_id = ?
            """,
            (status, time.time(), thread_id),
        )
        await self.conn.commit()

    async def set_support_action_message(
        self, thread_id: int, message_id: int
    ) -> None:
        """最新のボタン付きメッセージ ID を保存する。"""
        await self.conn.execute(
            """
            UPDATE support_threads
            SET last_action_message_id = ?, updated_at = ?
            WHERE thread_id = ?
            """,
            (message_id, time.time(), thread_id),
        )
        await self.conn.commit()

    async def bump_support_turn(self, thread_id: int) -> int:
        """往復数を1増やして返す。"""
        await self.conn.execute(
            """
            UPDATE support_threads
            SET turn_count = turn_count + 1, updated_at = ?
            WHERE thread_id = ?
            """,
            (time.time(), thread_id),
        )
        await self.conn.commit()
        state = await self.get_support_thread(thread_id)
        return int(state.get("turn_count") or 0)
