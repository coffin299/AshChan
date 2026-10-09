"""LLM のストリーミング出力を Discord メッセージへ段階的に反映する。"""

from __future__ import annotations

import logging
import time

import discord

log = logging.getLogger(__name__)

# 1メッセージに書き込む最大文字数（Discord 上限 2000 に余裕を持たせる）
MESSAGE_LIMIT = 1900


class StreamView:
    """一定間隔で返信メッセージを書き換え、長文は分割して送る。"""

    def __init__(
        self,
        origin: discord.Message,
        interval: float,
        limit: int = MESSAGE_LIMIT,
    ) -> None:
        # 返信先となるユーザーのメッセージ
        self._origin = origin
        # 書き換え間隔（秒）
        self._interval = max(interval, 0.5)
        # 1メッセージあたりの文字数上限
        self._limit = limit
        # 書き込み中のメッセージ（まだ送っていなければ None）
        self._current: discord.Message | None = None
        # 送信済みメッセージがあるか（最初の1通だけリプライにする）
        self._sent_any = False
        # 現在のメッセージに表示する本文
        self._text = ""
        # 本文の末尾に添えるステータス行（検索中表示など）
        self._status: str | None = None
        # 最後に Discord へ書き込んだ内容と時刻
        self._last_rendered = ""
        self._last_edit = 0.0
        # 全メッセージを通した本文（履歴保存用）
        self.full_text = ""

    @property
    def has_output(self) -> bool:
        """何か1文字でも本文を出力したか。"""
        return bool(self.full_text.strip())

    async def append(self, delta: str) -> None:
        """本文の差分を追加し、間隔が空いていれば書き換える。"""
        # 本文バッファに追加する
        self._text += delta
        self.full_text += delta
        # 上限を超えた分は新しいメッセージへ送り出す
        await self._roll_over()
        # 前回の書き換えから interval 経過していれば反映する
        if time.monotonic() - self._last_edit >= self._interval:
            await self._flush()

    async def set_status(self, status: str | None) -> None:
        """ステータス行を設定（None で消去）し、即座に反映する。"""
        # ステータスを差し替える
        self._status = status
        # 状態の変化はすぐ見せたいので間隔を無視して書き換える
        await self._flush()

    async def finalize(self, suffix: str = "") -> None:
        """ステータスを消し、末尾の追記を加えて最終内容を書き込む。"""
        # 検索中表示などを消す
        self._status = None
        # 参照リンクなどの追記を本文に足す（履歴には含めない）
        if suffix:
            self._text += suffix
            await self._roll_over()
        # 最終内容を反映する
        await self._flush()

    def _render(self) -> str:
        """現在のメッセージに表示する内容を組み立てる。"""
        # 本文とステータス行を改行でつなぐ
        body = self._text.rstrip()
        if self._status:
            return f"{body}\n{self._status}" if body else self._status
        return body

    async def _roll_over(self) -> None:
        """本文が上限を超えたら現メッセージを確定し、続きを次へ回す。"""
        while len(self._text) > self._limit:
            # なるべく改行位置で区切る（無ければ上限位置で切る）
            cut = self._text.rfind("\n", 0, self._limit)
            if cut < self._limit // 2:
                cut = self._limit
            head = self._text[:cut]
            # 残りを次のメッセージの本文にする
            self._text = self._text[cut:].lstrip("\n")
            # 現メッセージを確定内容で書き込み、次は新規メッセージにする
            await self._write(head)
            self._current = None
            self._last_rendered = ""

    async def _flush(self) -> None:
        """現在の内容を Discord に書き込む（変化が無ければ何もしない）。"""
        content = self._render()
        # 空・未変更なら API を呼ばない
        if not content or content == self._last_rendered:
            return
        await self._write(content)
        self._last_rendered = content

    async def _write(self, content: str) -> None:
        """現メッセージを編集、無ければ新規送信する。"""
        # 本文由来のメンションで誰かを呼ばないようにする
        mentions = discord.AllowedMentions.none()
        try:
            if self._current is None:
                # 最初の1通はリプライ、2通目以降は通常送信
                if not self._sent_any:
                    self._current = await self._origin.reply(
                        content, mention_author=False, allowed_mentions=mentions
                    )
                else:
                    self._current = await self._origin.channel.send(
                        content, allowed_mentions=mentions
                    )
                self._sent_any = True
            else:
                # 既存メッセージを書き換える
                await self._current.edit(content=content, allowed_mentions=mentions)
        except discord.HTTPException as exc:
            # 一時的な失敗で生成全体を止めないようにログだけ残す
            log.warning("ストリーミング表示の書き込みに失敗しました: %s", exc)
        # 書き込み時刻を記録する
        self._last_edit = time.monotonic()
