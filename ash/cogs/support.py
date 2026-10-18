"""VRCast サポートフォーラム: RAG トラブルシューティング。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from ash.bot import AshBot, DB_PATH
from ash.llm.client import LLMError
from ash.llm.support_status import (
    PHASE_ATTACHMENTS,
    PHASE_LLM,
    PHASE_RAG,
    PHASE_RETRY,
    PHASE_START,
    SupportFailureView,
    SupportReplyView,
    SupportStatusMessage,
    strip_message_buttons,
)
from ash.notifier import COLOR_DANGER, COLOR_INFO, make_embed
from ash.rag.embedder import EmbedError, Embedder
from ash.rag.html_text import html_to_text
from ash.rag.indexer import RagIndexer, RagJobCancelled
from ash.rag.retriever import RagRetriever
from ash.rag.store import RagStore

log = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

# /ash rag ingest で取り込む添付の拡張子
INGEST_EXTENSIONS = (
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".csv",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
)

DEFAULT_SUPPORT_PROMPT = """
## サポートモード
- VRCast のエンドユーザー向けトラブルシューティング担当。
- 初回は軽い自己紹介のあと、状況整理と不足情報（バージョン、再現手順、出力 txt、ログ）を求める。
- 正確性を優先。分からないことは知ったかぶりしない。
- ソースやドキュメントの根拠は、ユーザー向けの平易な言葉に言い換える。長い生コードは貼らない。
- 解決できそうになければ、下の「開発者に聞く」ボタンを案内する。
"""


class SupportCog(commands.Cog):
    """フォーラムサポート + RAG ジョブ管理。"""

    def __init__(self, bot: AshBot) -> None:
        self.bot = bot
        self._rag_store = RagStore(DB_PATH)
        self._retriever: RagRetriever | None = None
        self._job_task: asyncio.Task[None] | None = None
        self._job_indexer: RagIndexer | None = None
        self._busy_threads: set[int] = set()

    async def cog_load(self) -> None:
        await self._rag_store.connect()

    async def cog_unload(self) -> None:
        if self._job_indexer is not None:
            self._job_indexer.request_cancel()
        if self._job_task and not self._job_task.done():
            self._job_task.cancel()
            try:
                await self._job_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._retriever and self._retriever.embedder:
            try:
                await self._retriever.embedder.close()
            except Exception:
                log.debug("embedder close on unload failed", exc_info=True)
        try:
            await self._rag_store.close()
        except Exception:
            log.debug("rag store close on unload failed", exc_info=True)

    # ------------------------------------------------------------
    # 公開: チャット Cog からの判定
    # ------------------------------------------------------------
    def is_support_thread(self, channel: discord.abc.Messageable) -> bool:
        """サポート対象フォーラムのスレッドか。"""
        cfg = self.bot.config.support
        if not cfg.enabled:
            return False
        if not isinstance(channel, discord.Thread):
            return False
        parent = channel.parent
        if parent is None:
            return False
        return parent.id in cfg.forum_channel_ids

    # ------------------------------------------------------------
    # RAG ジョブ（admin / github から利用）
    # ------------------------------------------------------------
    @property
    def rag_busy(self) -> bool:
        return self._job_task is not None and not self._job_task.done()

    def request_rag_cancel(self) -> bool:
        if self._job_indexer is not None:
            self._job_indexer.request_cancel()
            return True
        return False

    async def rag_status(self) -> dict[str, Any]:
        return await self._rag_store.status()

    def invalidate_retriever(self) -> None:
        if self._retriever is not None:
            self._retriever.invalidate()

    async def start_rag_job(
        self,
        kind: str,
        *,
        force_nim: bool = False,
        to_tag: str | None = None,
        history_limit: int = 0,
        include_archived: bool = True,
    ) -> str:
        """kind: rebuild-nim | sync | history。二重実行時はエラー文字列。"""
        if self.rag_busy:
            return "already_running"
        self.bot.presence.set_rag(kind)
        asyncio.create_task(self.bot.presence.bump(), name="presence-rag-start")
        self._job_task = asyncio.create_task(
            self._run_rag_job(
                kind,
                force_nim=force_nim,
                to_tag=to_tag,
                history_limit=history_limit,
                include_archived=include_archived,
            ),
            name=f"rag-{kind}",
        )
        return "started"

    async def ingest_message_documents(
        self,
        *,
        channel: discord.abc.Messageable,
        message_id: int,
    ) -> dict[str, Any]:
        """指定メッセージのテキスト添付を RAG に取り込む。"""
        if self.rag_busy:
            return {"ok": False, "error": "already_running"}
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return {"ok": False, "error": "message_not_found"}
        except discord.HTTPException as exc:
            return {"ok": False, "error": f"fetch_failed: {exc}"}

        files = self._ingestible_attachments(message)
        if not files:
            return {
                "ok": False,
                "error": "no_documents",
                "hint": "対応拡張子: " + ", ".join(sorted(INGEST_EXTENSIONS)),
            }

        cfg = self.bot.config.support
        indexer = RagIndexer(
            self._rag_store,
            cfg,
            api_key=cfg.embedding_api_key or self.bot.config.llm.api_key,
            base_url=cfg.embedding_base_url or self.bot.config.llm.base_url,
            model=cfg.embedding_model,
        )
        indexed: list[str] = []
        skipped: list[str] = []
        chunk_total = 0
        try:
            for att in files:
                text = await self._read_attachment_text(att)
                if text is None or not text.strip():
                    skipped.append(att.filename or "(unnamed)")
                    continue
                n = await indexer.index_manual_document(
                    message_id=message_id,
                    filename=att.filename or "document.txt",
                    text=text,
                )
                if n <= 0:
                    skipped.append(att.filename or "(unnamed)")
                    continue
                chunk_total += n
                indexed.append(f"{att.filename} ({n})")
        except (EmbedError, RagJobCancelled) as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            await indexer.close()

        if not indexed:
            return {
                "ok": False,
                "error": "empty_or_unreadable",
                "skipped": skipped,
            }
        self.invalidate_retriever()
        return {
            "ok": True,
            "chunks": chunk_total,
            "files": indexed,
            "skipped": skipped,
            "message_id": message_id,
        }

    async def _run_rag_job(
        self,
        kind: str,
        *,
        force_nim: bool,
        to_tag: str | None,
        history_limit: int = 0,
        include_archived: bool = True,
    ) -> None:
        cfg = self.bot.config.support
        api_key = cfg.embedding_api_key
        base_url = cfg.embedding_base_url
        model = cfg.embedding_model
        if force_nim or kind == "rebuild-nim":
            base_url = "https://integrate.api.nvidia.com/v1"
            model = (
                model if "nvidia/" in model else "nvidia/nemotron-3-embed-1b"
            )
            api_key = (
                self.bot.config.llm.api_key
                or cfg.embedding_api_key
            )
        indexer = RagIndexer(
            self._rag_store,
            cfg,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        self._job_indexer = indexer

        async def on_progress(message: str, done: int, total: int) -> None:
            if done == total or done % 25 == 0:
                log.info("RAG %s: %s (%s/%s)", kind, message, done, total)

        try:
            if kind == "rebuild-nim":
                tag = to_tag or await indexer.fetch_latest_release_tag()
                n = await indexer.bootstrap(
                    tag=tag, force=True, on_progress=on_progress
                )
                summary = f"rebuild-nim 完了 tag=`{tag}` chunks={n}"
            elif kind == "history":
                threads_n, chunk_n = await self._index_support_history(
                    indexer,
                    limit=history_limit,
                    include_archived=include_archived,
                    on_progress=on_progress,
                )
                summary = (
                    f"history 完了 threads={threads_n} chunks={chunk_n}"
                )
            else:
                prev, target, n = await indexer.compare_and_sync(
                    to_tag=to_tag, on_progress=on_progress
                )
                summary = f"sync 完了 {prev or '-'}→`{target}` chunks≈{n}"
            self.invalidate_retriever()
            await self.bot.notifier.notify_dev(
                make_embed("RAG 完了", summary, COLOR_INFO)
            )
        except RagJobCancelled:
            await self.bot.notifier.notify_dev(
                make_embed("RAG キャンセル", f"{kind} を中断しました。", COLOR_INFO)
            )
        except Exception as exc:
            log.exception("RAG job failed")
            await self.bot.notifier.notify_dev(
                make_embed(
                    "RAG 失敗",
                    f"```\n{exc}\n```",
                    COLOR_DANGER,
                    {"kind": kind},
                )
            )
        finally:
            await indexer.close()
            self._job_indexer = None
            self.bot.presence.set_rag(None)
            await self.bot.presence.bump()

    async def _index_support_history(
        self,
        indexer: RagIndexer,
        *,
        limit: int,
        include_archived: bool,
        on_progress: Any,
    ) -> tuple[int, int]:
        """設定フォーラムの過去スレ（解決済み含む）を RAG に載せる。"""
        cfg = self.bot.config.support
        forum_ids = list(cfg.forum_channel_ids)
        if not forum_ids:
            raise EmbedError("support.forum_channel_ids が空です")

        threads: list[discord.Thread] = []
        for forum_id in forum_ids:
            channel = self.bot.get_channel(forum_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(forum_id)
                except discord.HTTPException as exc:
                    log.warning("history: forum %s fetch failed: %s", forum_id, exc)
                    continue
            if not isinstance(channel, discord.ForumChannel):
                log.warning("history: %s is not a forum channel", forum_id)
                continue
            for t in channel.threads:
                threads.append(t)
            if include_archived:
                try:
                    async for t in channel.archived_threads(limit=None):
                        threads.append(t)
                except discord.HTTPException as exc:
                    log.warning(
                        "history: archived_threads failed for %s: %s",
                        forum_id,
                        exc,
                    )

        # 重複排除（アクティブとアーカイブで二重に来ることは稀だが念のため）
        seen: set[int] = set()
        unique: list[discord.Thread] = []
        for t in threads:
            if t.id in seen:
                continue
            seen.add(t.id)
            unique.append(t)

        # 新しい順 → 古い順どちらでもよいが、limit 用に新しい方を優先
        unique.sort(key=lambda t: t.id, reverse=True)
        if limit > 0:
            unique = unique[:limit]

        total = len(unique)
        done = 0
        chunk_total = 0
        indexed_threads = 0
        for thread in unique:
            indexer._check_cancel()
            body = await self._format_thread_transcript(thread)
            if not body.strip():
                done += 1
                if on_progress:
                    result = on_progress(f"skip {thread.id}", done, total)
                    if asyncio.iscoroutine(result):
                        await result
                continue
            status = await self._resolve_thread_status(thread)
            n = await indexer.index_support_history_thread(
                thread_id=thread.id,
                title=thread.name or f"thread-{thread.id}",
                status=status,
                body=body,
            )
            chunk_total += n
            if n > 0:
                indexed_threads += 1
            done += 1
            if on_progress:
                result = on_progress(
                    f"thread {thread.name}", done, total
                )
                if asyncio.iscoroutine(result):
                    await result

        await self._rag_store.set_meta(
            "support_history_indexed_at",
            str(time.time()),
        )
        return indexed_threads, chunk_total

    async def _resolve_thread_status(self, thread: discord.Thread) -> str:
        """DB / フォーラムタグから解決状態を推定する。"""
        state = await self.bot.db.get_support_thread(thread.id)
        db_status = str(state.get("status") or "").strip()
        if db_status:
            return db_status
        tag_name = self.bot.config.support.resolved_tag_name.strip()
        if tag_name:
            for tag in thread.applied_tags:
                if tag.name == tag_name:
                    return "resolved"
        if thread.archived:
            return "archived"
        return "open"

    async def _format_thread_transcript(
        self, thread: discord.Thread, *, message_limit: int = 150
    ) -> str:
        """スレ会話を RAG 用テキストにまとめる。"""
        max_chars = self.bot.config.support.max_file_chars
        lines: list[str] = []
        used = 0
        try:
            async for message in thread.history(
                limit=message_limit, oldest_first=True
            ):
                if used >= max_chars:
                    break
                author = getattr(message.author, "display_name", None) or "user"
                bot_mark = " [bot]" if message.author.bot else ""
                content = (message.content or "").strip()
                att_names = [
                    a.filename
                    for a in message.attachments
                    if a.filename
                ]
                if not content and not att_names:
                    continue
                entry = (
                    f"**{author}{bot_mark}**: {content}"
                    if content
                    else f"**{author}{bot_mark}**:"
                )
                if att_names:
                    entry += f"\n(添付: {', '.join(att_names)})"
                remain = max_chars - used
                if len(entry) > remain:
                    entry = entry[:remain]
                lines.append(entry)
                used += len(entry) + 1
        except discord.HTTPException as exc:
            log.warning("transcript failed for %s: %s", thread.id, exc)
            return ""
        return "\n\n".join(lines)

    @staticmethod
    def _ingestible_attachments(
        message: discord.Message,
    ) -> list[discord.Attachment]:
        out: list[discord.Attachment] = []
        for att in message.attachments:
            name = (att.filename or "").lower()
            if any(name.endswith(ext) for ext in INGEST_EXTENSIONS):
                out.append(att)
        return out

    async def _read_attachment_text(
        self, att: discord.Attachment
    ) -> str | None:
        """添付をテキストとして読む（HTML はプレーン化）。"""
        max_chars = self.bot.config.support.max_file_chars
        try:
            raw = await att.read()
        except (aiohttp.ClientError, discord.HTTPException):
            return None
        if b"\0" in raw[:8000]:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
        name = (att.filename or "").lower()
        if name.endswith((".html", ".htm")):
            text = html_to_text(text)
        return text[:max_chars]

    def _forum_name(self, thread: discord.Thread) -> str:
        """親フォーラムの表示名を返す。"""
        parent = thread.parent
        if parent is not None:
            return parent.name
        return thread.name or "forum"

    async def on_release_published(self, repo: str, tag: str, body: str) -> None:
        """GitHub リリース公開時の差分インデックス。"""
        cfg = self.bot.config.support
        if not cfg.enabled:
            return
        if repo.lower() != cfg.repo.lower():
            return
        if self.rag_busy:
            log.warning("skip release RAG sync; job already running")
            return
        await self.start_rag_job("sync", to_tag=tag)
        # リリースノートも別途（sync 完了後が理想だが簡易に並行しない）
        # sync タスク内で tag が進むので、ノートは sync 後に載せたい。
        # ここではジョブ完了待ちせず、ノートだけ即インデックスを試みる。
        try:
            indexer = RagIndexer(self._rag_store, cfg)
            await indexer.index_release_notes(tag, body or "")
            await indexer.close()
            self.invalidate_retriever()
        except Exception:
            log.exception("release notes index failed")

    # ------------------------------------------------------------
    # フォーラムイベント
    # ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not self.is_support_thread(thread):
            return
        await asyncio.sleep(1.0)
        starter = thread.starter_message
        if starter is None:
            try:
                starter = await thread.fetch_message(thread.id)
            except discord.HTTPException:
                return
        await self._handle_user_message(starter, first_turn=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if not self.is_support_thread(message.channel):
            return
        # スレ作成の starter は on_thread_create で処理
        if isinstance(message.channel, discord.Thread) and message.id == message.channel.id:
            return
        if not self._is_reply_to_bot(message):
            return
        await self._handle_user_message(message, first_turn=False)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith("ash:support:"):
            return
        parts = custom_id.split(":")
        # ash:support:resolve:THREAD[:dead]
        if len(parts) < 4:
            return
        action = parts[2]
        try:
            thread_id = int(parts[3])
        except ValueError:
            return
        if parts[-1] == "dead":
            await interaction.response.send_message(
                "この操作はもう使えないよ。", ephemeral=True
            )
            return
        if action == "resolve":
            await self._on_resolve(interaction, thread_id)
        elif action == "escalate":
            await self._on_escalate(interaction, thread_id)

    def _is_reply_to_bot(self, message: discord.Message) -> bool:
        me = self.bot.user
        if me is None:
            return False
        ref = message.reference
        if ref is None:
            return False
        target = ref.resolved or ref.cached_message
        return isinstance(target, discord.Message) and target.author.id == me.id

    async def _handle_user_message(
        self, message: discord.Message, *, first_turn: bool
    ) -> None:
        cfg = self.bot.config.support
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        thread_id = channel.id
        if thread_id in self._busy_threads:
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass
            return

        state = await self.bot.db.ensure_support_thread(
            thread_id, message.author.id
        )
        status = str(state.get("status") or "open")
        if status in ("resolved", "failed"):
            return

        self._busy_threads.add(thread_id)
        forum_name = self._forum_name(channel)
        self.bot.presence.begin_support(thread_id, forum_name)
        await self.bot.presence.bump()
        status_msg = SupportStatusMessage(message)
        try:
            await status_msg.set_phase(PHASE_START)
            answer = await self._run_with_retries(
                message, status_msg, first_turn=first_turn
            )
            if answer is None:
                return
            await self._strip_previous_buttons(channel, state)
            view = SupportReplyView(
                body=answer,
                footer_hint=cfg.footer_hint,
                thread_id=thread_id,
                resolve_label=cfg.resolve_button_label,
                escalate_label=cfg.escalate_button_label,
                enabled=True,
            )
            final = await status_msg.finalize_success(view)
            await self.bot.db.set_support_action_message(thread_id, final.id)
            user_turn = self._format_user_turn(message, first_turn=first_turn)
            await self.bot.db.add_history(thread_id, "user", user_turn)
            await self.bot.db.add_history(thread_id, "assistant", answer)
            await self.bot.db.trim_history(
                thread_id, self.bot.config.llm.history_turns * 2
            )
            await self.bot.db.bump_support_turn(thread_id)
        finally:
            self._busy_threads.discard(thread_id)
            self.bot.presence.end_support(thread_id)
            await self.bot.presence.bump()

    async def _run_with_retries(
        self,
        message: discord.Message,
        status_msg: SupportStatusMessage,
        *,
        first_turn: bool,
    ) -> str | None:
        cfg = self.bot.config.support
        attempts = max(1, cfg.llm_retry_count)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._generate_answer(
                    message, status_msg, first_turn=first_turn
                )
            except (LLMError, EmbedError, OSError, aiohttp.ClientError) as exc:
                last_error = exc
                log.warning(
                    "support attempt %s/%s failed: %s", attempt, attempts, exc
                )
                if attempt >= attempts:
                    break
                await status_msg.set_phase(PHASE_RETRY)
                await asyncio.sleep(cfg.llm_retry_delay_sec)
        await self._finalize_failure(message, status_msg, last_error)
        return None

    async def _generate_answer(
        self,
        message: discord.Message,
        status_msg: SupportStatusMessage,
        *,
        first_turn: bool,
    ) -> str:
        cfg = self.bot.config.support
        await status_msg.set_phase(PHASE_ATTACHMENTS)
        attachments = await self._read_attachments(message)
        query = self._format_user_turn(message, first_turn=first_turn)
        if attachments:
            query = f"{query}\n\n[attachments]\n{attachments}"

        await status_msg.set_phase(PHASE_RAG)
        context = ""
        try:
            retriever = await self._get_retriever()
            hits = await retriever.retrieve(query)
            context = RagRetriever.format_context(hits)
        except EmbedError as exc:
            log.warning("RAG retrieve failed: %s", exc)
            # インデックス無しでも会話は続ける
            context = ""

        await status_msg.set_phase(PHASE_LLM)
        history = await self.bot.db.get_history(
            message.channel.id, self.bot.config.llm.history_turns * 2
        )
        system = self._system_prompt(first_turn=first_turn, has_context=bool(context))
        user_content = query
        if context:
            user_content = (
                f"{query}\n\n## Retrieved knowledge\n{context}"
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_content},
        ]
        answer = await self.bot.llm.complete(messages)
        if not answer.strip():
            raise LLMError("empty support response")
        return answer.strip()[:3800]

    async def _finalize_failure(
        self,
        message: discord.Message,
        status_msg: SupportStatusMessage,
        error: Exception | None,
    ) -> None:
        cfg = self.bot.config.support
        thread = message.channel
        assert isinstance(thread, discord.Thread)
        await self.bot.db.set_support_status(thread.id, "failed")
        body = (
            "ごめん、いま LLM サポートがうまく動かなくて失敗しちゃった。"
            "開発者にバトン渡すね。しばらく待ってて。"
        )
        if error:
            log.error("support failed permanently: %s", error)
        view = SupportFailureView(
            body=body,
            thread_id=thread.id,
            resolve_label=cfg.resolve_button_label,
            escalate_label=cfg.escalate_button_label,
        )
        # Components V2 は content と併用できないので、メンションは別メッセージ
        await status_msg.finalize_failure(view)
        mentions = " ".join(f"<@{uid}>" for uid in cfg.admin_user_ids)
        if mentions:
            try:
                await thread.send(
                    f"{mentions}\nLLM サポートに失敗したよ。手動で見てほしい。",
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            except discord.HTTPException:
                pass
        await self.bot.notifier.notify_dev(
            make_embed(
                "サポート LLM 失敗",
                f"thread={thread.jump_url}\n```\n{error}\n```",
                COLOR_DANGER,
            )
        )

    async def _strip_previous_buttons(
        self, thread: discord.Thread, state: dict[str, Any]
    ) -> None:
        msg_id = int(state.get("last_action_message_id") or 0)
        if not msg_id:
            return
        try:
            old = await thread.fetch_message(msg_id)
        except discord.HTTPException:
            return
        body = old.content or ""
        # V2 メッセージは content が空のことがある → コンポーネントから取れないので履歴を使う
        if not body.strip():
            history = await self.bot.db.get_history(thread.id, 2)
            for item in reversed(history):
                if item["role"] == "assistant":
                    body = item["content"]
                    break
        if not body.strip():
            body = "（前回の返信）"
        await strip_message_buttons(old, body)

    async def _on_resolve(
        self, interaction: discord.Interaction, thread_id: int
    ) -> None:
        cfg = self.bot.config.support
        thread = interaction.channel
        if not isinstance(thread, discord.Thread) or thread.id != thread_id:
            await interaction.response.send_message(
                "スレ情報を取れなかったよ。", ephemeral=True
            )
            return
        state = await self.bot.db.get_support_thread(thread_id)
        owner_id = int(state.get("owner_id") or 0)
        member = interaction.user
        is_staff = False
        if isinstance(member, discord.Member):
            is_staff = member.guild_permissions.manage_threads or member.guild_permissions.administrator
        if member.id != owner_id and not is_staff:
            await interaction.response.send_message(
                "スレ主かスタッフだけが解決にできるよ。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_support_status(thread_id, "resolved")
        tag_name = cfg.resolved_tag_name.strip()
        if tag_name and thread.parent and isinstance(thread.parent, discord.ForumChannel):
            available = {t.name: t for t in thread.parent.available_tags}
            tag = available.get(tag_name)
            if tag is not None:
                tags = list(thread.applied_tags)
                if tag not in tags:
                    tags.append(tag)
                    try:
                        await thread.edit(applied_tags=tags[:5])
                    except discord.HTTPException:
                        pass
        try:
            await thread.edit(archived=True)
        except discord.HTTPException:
            pass
        # 最新メッセージのボタンを外す
        msg_id = int(state.get("last_action_message_id") or 0)
        if msg_id and interaction.message and interaction.message.id == msg_id:
            try:
                await strip_message_buttons(
                    interaction.message,
                    "解決済みとしてクローズしたよ。また何かあったらスレ立ててね。",
                )
            except Exception:
                pass
        await interaction.followup.send("解決として閉じたよ。お疲れ！", ephemeral=True)

    async def _on_escalate(
        self, interaction: discord.Interaction, thread_id: int
    ) -> None:
        cfg = self.bot.config.support
        thread = interaction.channel
        if not isinstance(thread, discord.Thread) or thread.id != thread_id:
            await interaction.response.send_message(
                "スレ情報を取れなかったよ。", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.bot.db.set_support_status(thread_id, "escalated")
        self.bot.presence.begin_support(thread_id, self._forum_name(thread))
        await self.bot.presence.bump()
        try:
            brief = await self._build_developer_brief(thread)
        finally:
            self.bot.presence.end_support(thread_id)
            await self.bot.presence.bump()

        mentions = " ".join(f"<@{uid}>" for uid in cfg.admin_user_ids) or "（管理者未設定）"
        header = f"{mentions}\n**開発者エスカレーション** — `{thread.name}`"
        # Discord 2000 文字制限: ヘッダ + 本文を分割送信
        await thread.send(
            header,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        for chunk in self._split_discord_chunks(brief, limit=1900):
            await thread.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if interaction.message:
            hist = await self.bot.db.get_history(thread_id, 2)
            body = "開発者にエスカレーション済み。詳細は上のまとめを見てね。"
            for item in reversed(hist):
                if item["role"] == "assistant":
                    body = item["content"]
                    break
            try:
                await strip_message_buttons(interaction.message, body)
            except Exception:
                pass
        await interaction.followup.send(
            "開発者向けのまとめを出してメンションしたよ。", ephemeral=True
        )

    async def _build_developer_brief(self, thread: discord.Thread) -> str:
        """会話履歴・ログ・RAG から開発者向け引き継ぎメモを作る。"""
        history = await self.bot.db.get_history(thread.id, 40)
        history_text = "\n".join(
            f"{item['role']}: {item['content']}" for item in history
        ) or "（履歴なし）"
        logs = await self._collect_thread_logs(thread)
        logs_block = logs if logs.strip() else "（.txt / .log 添付なし、または読めず）"

        # 症状に近いコード片を拾って修正方針の根拠にする
        rag_block = ""
        try:
            retriever = await self._get_retriever()
            query = (
                f"{thread.name}\n{history_text[-4000:]}\n{logs_block[:2000]}"
            ).strip()
            hits = await retriever.retrieve(query)
            rag_block = RagRetriever.format_context(hits)
        except EmbedError as exc:
            log.warning("developer brief RAG failed: %s", exc)

        system = (
            "あなたは VRCast のサポートから開発者への引き継ぎ担当。"
            "口調は簡潔な技術メモ（ですます調でも箇条書きでも可）。"
            "キャラ演技は不要。絵文字も不要。"
            "ユーザー向け説明ではなく、開発者がすぐ着手できる情報をまとめる。"
            "コード修正案は「方向性・調査ポイント」まで。"
            "完成パッチや断定的な一行修正は書かない。根拠が弱い箇所は推測と明記する。"
        )
        rag_section = (
            f"## 参照コード／ドキュメント（RAG）\n{rag_block[:12000]}\n\n"
            if rag_block.strip()
            else "## 参照コード／ドキュメント（RAG）\n（ヒットなし）\n\n"
        )
        user = (
            f"フォーラムスレ名: {thread.name}\n"
            f"スレURL: {thread.jump_url}\n\n"
            "## 会話履歴\n"
            f"{history_text[:12000]}\n\n"
            "## 添付ログ抜粋\n"
            f"{logs_block[:10000]}\n\n"
            f"{rag_section}"
            "上記から開発者向け引き継ぎメモを日本語で作成せよ。必ず次の見出しを含める:\n"
            "### 症状まとめ\n"
            "### 環境・バージョン（分かれば）\n"
            "### 再現手順（分かれば）\n"
            "### ログ・添付からの要点\n"
            "### ボットが試したこと / 案内したこと\n"
            "### 未解決点・確認してほしいこと\n"
            "### 関連しそうな機能・ファイル（推測可・断定しない）\n"
            "### コード修正案の方向性\n"
            "（必須。RAG や症状から、どこをどう直しそうかの方針を2〜5点。"
            "例: 対象ファイル／クラス、疑わしい分岐、追加すべきガード、ログ確認箇所。"
            "確定パッチは書かず「要確認」「推測」を付ける。"
            "根拠が無ければ「追加調査が必要」と書く。）\n"
            "不明な項目は「不明」と書く。推測は推測と明記。"
            "全体はだいたい1500〜2200文字。"
        )
        try:
            brief = await self.bot.llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
            if brief.strip():
                return brief.strip()[:4500]
        except LLMError as exc:
            log.warning("developer brief LLM failed: %s", exc)

        # LLM 失敗時は履歴をそのまま圧縮して渡す
        fallback = [
            "### 症状まとめ",
            f"- スレ: {thread.name}",
            "- （自動要約に失敗したため生ログを添付）",
            "### 会話履歴（抜粋）",
            history_text[:1500] or "（なし）",
            "### ログ・添付からの要点",
            logs_block[:800],
            "### 未解決点・確認してほしいこと",
            "- 上記を直接確認してください",
            "### コード修正案の方向性",
            "- （自動生成失敗のため未作成。RAG/ログを直接確認してください）",
        ]
        return "\n".join(fallback)

    async def _collect_thread_logs(self, thread: discord.Thread) -> str:
        """スレ内の直近メッセージから .txt / .log を集める。"""
        limit = self.bot.config.support.max_attachment_chars
        parts: list[str] = []
        used = 0
        try:
            async for message in thread.history(limit=40, oldest_first=True):
                if used >= limit:
                    break
                # 履歴 DB に無い添付も拾う
                for att in message.attachments:
                    name = (att.filename or "").lower()
                    if not (name.endswith(".txt") or name.endswith(".log")):
                        continue
                    remain = limit - used
                    if remain <= 0:
                        break
                    try:
                        raw = await att.read()
                    except (aiohttp.ClientError, discord.HTTPException):
                        continue
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = raw.decode("cp932", errors="replace")
                    # 先頭と末尾を優先（長いログ向け）
                    clip = self._clip_log(text, remain)
                    parts.append(
                        f"### {att.filename} (by {message.author.display_name})\n{clip}"
                    )
                    used += len(clip)
        except discord.HTTPException as exc:
            log.warning("collect_thread_logs failed: %s", exc)
        return "\n\n".join(parts)

    @staticmethod
    def _clip_log(text: str, max_chars: int) -> str:
        """長いログは先頭＋末尾を残す。"""
        text = text.replace("\r\n", "\n")
        if len(text) <= max_chars:
            return text
        head = max_chars // 2
        tail = max_chars - head - 20
        return f"{text[:head]}\n\n…(省略)…\n\n{text[-tail:]}"

    @staticmethod
    def _split_discord_chunks(text: str, *, limit: int = 1900) -> list[str]:
        """長文を Discord メッセージ長に分割する。"""
        text = text.strip()
        if not text:
            return ["（まとめなし）"]
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        rest = text
        while rest:
            if len(rest) <= limit:
                chunks.append(rest)
                break
            cut = rest.rfind("\n", 0, limit)
            if cut < limit // 3:
                cut = limit
            chunks.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        return chunks

    async def _get_retriever(self) -> RagRetriever:
        if self._retriever is None:
            cfg = self.bot.config.support
            embedder = Embedder(
                api_key=cfg.embedding_api_key or self.bot.config.llm.api_key,
                base_url=cfg.embedding_base_url or self.bot.config.llm.base_url,
                model=cfg.embedding_model,
                batch_size=cfg.embed_batch_size,
                rpm=cfg.embed_rpm,
            )
            self._retriever = RagRetriever(
                self._rag_store, embedder, top_k=cfg.top_k
            )
        return self._retriever

    def _system_prompt(self, *, first_turn: bool, has_context: bool) -> str:
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M (JST)")
        support_extra = self._read_support_prompt()
        notes = [
            self.bot.system_prompt,
            support_extra or DEFAULT_SUPPORT_PROMPT,
            f"現在日時: {now}",
        ]
        if first_turn:
            notes.append(
                "これはスレの最初の返信。短い自己紹介のあと、状況の整理と"
                "不足情報（バージョン・再現手順・出力txt・ログ）を聞いて。"
            )
        else:
            notes.append("続きの会話。自己紹介は不要。")
        if has_context:
            notes.append(
                "Retrieved knowledge を根拠に答える。出典パス名を必要なら軽く触れてよいが、"
                "ユーザー向けに噛み砕くこと。"
            )
        else:
            notes.append("ナレッジが空でも、一般知識と聞き取りでサポートする。")
        turns_hint = self.bot.config.support.max_turns_before_hint
        notes.append(
            f"往復が多く難航しているときは「開発者に聞く」ボタンを案内してよい"
            f"（目安 {turns_hint} 往復）。"
        )
        notes.append("返答は Discord Markdown、だいたい1500文字以内。")
        return "\n\n".join(notes)

    def _read_support_prompt(self) -> str:
        path = self.bot.config.resolve_path(self.bot.config.support.prompt_file)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _format_user_turn(
        self, message: discord.Message, *, first_turn: bool
    ) -> str:
        text = message.content or ""
        me = self.bot.user
        if me is not None:
            text = re.sub(rf"<@!?{me.id}>", "", text)
        text = text.strip()
        if isinstance(message.channel, discord.Thread) and first_turn:
            title = message.channel.name
            text = f"[thread: {title}]\n{text}".strip()
        if not text:
            text = "（本文なし）"
        return f"{message.author.display_name}: {text}"

    async def _read_attachments(self, message: discord.Message) -> str:
        limit = self.bot.config.support.max_attachment_chars
        parts: list[str] = []
        used = 0
        for att in message.attachments:
            name = (att.filename or "").lower()
            if not (name.endswith(".txt") or name.endswith(".log")):
                continue
            try:
                raw = await att.read()
            except (aiohttp.ClientError, discord.HTTPException):
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp932", errors="replace")
            remain = limit - used
            if remain <= 0:
                break
            clip = text[:remain]
            parts.append(f"### {att.filename}\n{clip}")
            used += len(clip)
        return "\n\n".join(parts)


async def setup(bot: AshBot) -> None:
    await bot.add_cog(SupportCog(bot))
