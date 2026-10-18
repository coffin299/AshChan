"""リポジトリ全文 / 差分の RAG インデックス更新。"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import aiohttp

from ash.config import SupportConfig
from ash.rag.chunker import chunk_file
from ash.rag.embedder import EmbedError, Embedder
from ash.rag.html_text import html_to_text
from ash.rag.store import RagStore

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], Awaitable[None] | None]
GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
HELP_PATH_PREFIX = "__help__/"
MANUAL_PATH_PREFIX = "__manual__/"
SUPPORT_PATH_PREFIX = "__support__/"
# rebuild 時も残す（Discord 手動取り込み・過去サポート）
PRESERVED_PREFIXES = (MANUAL_PATH_PREFIX, SUPPORT_PATH_PREFIX)


class RagJobCancelled(Exception):
    """インデックスジョブがキャンセルされた。"""


class RagIndexer:
    """フル bootstrap とタグ差分更新（ローカル or GitHub リモート）。"""

    def __init__(
        self,
        store: RagStore,
        config: SupportConfig,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._cancelled = False
        self.embedder = Embedder(
            api_key=api_key or config.embedding_api_key,
            base_url=base_url or config.embedding_base_url,
            model=model or config.embedding_model,
            batch_size=config.embed_batch_size,
            rpm=config.embed_rpm,
        )
        self.embedder.set_cancel_check(lambda: self._cancelled)

    def request_cancel(self) -> None:
        self._cancelled = True

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise RagJobCancelled("RAG job cancelled")

    async def close(self) -> None:
        await self.embedder.close()

    def _local_root(self, repo_path: Path | None) -> Path | None:
        """使えるローカル clone があれば Path、無ければ None。"""
        if repo_path is not None:
            root = repo_path.expanduser().resolve()
        elif self.config.local_repo_path.strip():
            root = Path(self.config.local_repo_path).expanduser().resolve()
        else:
            return None
        return root if root.is_dir() else None

    async def bootstrap(
        self,
        *,
        repo_path: Path | None = None,
        tag: str,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """全文インデックス。force で既存チャンクを消して作り直す。

        手動取り込み（``__manual__/``）と過去サポート（``__support__/``）は残す。
        """
        if force:
            await self.store.clear_except_prefixes(list(PRESERVED_PREFIXES))
        root = self._local_root(repo_path)
        if root is not None:
            chunk_total = await self._bootstrap_local(root, tag, on_progress)
        else:
            log.info("RAG bootstrap via GitHub remote (%s @ %s)", self.config.repo, tag)
            chunk_total = await self._bootstrap_github(tag, on_progress)
        chunk_total += await self.index_help_site(tag=tag, on_progress=on_progress)
        await self.store.set_meta("current_tag", tag)
        await self.store.set_meta("embed_model", self.embedder.model)
        await self.store.set_meta("indexed_at", str(time.time()))
        return chunk_total

    async def _bootstrap_local(
        self,
        root: Path,
        tag: str,
        on_progress: ProgressCallback | None,
    ) -> int:
        files = list(self._iter_files(root))
        total = len(files)
        done = 0
        chunk_total = 0
        for rel, abs_path in files:
            self._check_cancel()
            text = self._read_text(abs_path)
            if text is None:
                done += 1
                continue
            n = await self._index_text(rel, text, tag)
            chunk_total += n
            done += 1
            await self._progress(on_progress, f"indexed {rel}", done, total)
        return chunk_total

    async def _bootstrap_github(
        self,
        tag: str,
        on_progress: ProgressCallback | None,
    ) -> int:
        paths = await self._github_list_paths(tag)
        total = len(paths)
        done = 0
        chunk_total = 0
        async with aiohttp.ClientSession() as session:
            for rel in paths:
                self._check_cancel()
                text = await self._github_fetch_file(session, tag, rel)
                if text is None:
                    done += 1
                    continue
                n = await self._index_text(rel, text, tag)
                chunk_total += n
                done += 1
                await self._progress(on_progress, f"indexed {rel}", done, total)
        return chunk_total

    async def apply_diff(
        self,
        *,
        repo_path: Path | None = None,
        from_tag: str,
        to_tag: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """from_tag..to_tag の差分だけ更新する。"""
        root = self._local_root(repo_path)
        if root is not None:
            changes = self._git_name_status(root, from_tag, to_tag)
            reader = lambda rel: self._read_text(root / rel) if (root / rel).is_file() else None
        else:
            log.info(
                "RAG diff via GitHub compare (%s %s..%s)",
                self.config.repo,
                from_tag,
                to_tag,
            )
            changes = await self._github_compare(from_tag, to_tag)
            reader = None  # set below with session

        if not changes:
            await self.store.set_meta("prev_tag", from_tag)
            await self.store.set_meta("current_tag", to_tag)
            await self.store.set_meta("indexed_at", str(time.time()))
            return 0

        total = len(changes)
        done = 0
        chunk_total = 0

        if root is not None:
            for status, rel in changes:
                self._check_cancel()
                if not self._path_included(rel):
                    done += 1
                    continue
                if status == "D":
                    await self.store.delete_path(rel)
                else:
                    text = reader(rel) if reader else None
                    if text is None:
                        await self.store.delete_path(rel)
                    else:
                        chunk_total += await self._index_text(rel, text, to_tag)
                done += 1
                await self._progress(on_progress, f"{status} {rel}", done, total)
        else:
            async with aiohttp.ClientSession() as session:
                for status, rel in changes:
                    self._check_cancel()
                    if not self._path_included(rel):
                        done += 1
                        continue
                    if status == "D":
                        await self.store.delete_path(rel)
                    else:
                        text = await self._github_fetch_file(session, to_tag, rel)
                        if text is None:
                            await self.store.delete_path(rel)
                        else:
                            chunk_total += await self._index_text(rel, text, to_tag)
                    done += 1
                    await self._progress(on_progress, f"{status} {rel}", done, total)

        await self.store.set_meta("prev_tag", from_tag)
        await self.store.set_meta("current_tag", to_tag)
        await self.store.set_meta("embed_model", self.embedder.model)
        await self.store.set_meta("indexed_at", str(time.time()))
        return chunk_total

    async def index_release_notes(self, tag: str, body: str) -> int:
        """リリースノート本文を仮想パスでインデックスする。"""
        if not body.strip():
            return 0
        path = f"__release__/{tag}.md"
        return await self._index_text(path, body, tag)

    async def index_manual_document(
        self,
        *,
        message_id: int,
        filename: str,
        text: str,
    ) -> int:
        """Discord 添付など手動ドキュメントをインデックスする。"""
        if not text.strip():
            return 0
        safe = (
            filename.replace("\\", "_")
            .replace("/", "_")
            .replace("\0", "")
            .strip()
            or "document.txt"
        )
        path = f"{MANUAL_PATH_PREFIX}{message_id}/{safe}"
        return await self._index_text(path, text, "manual")

    async def index_support_history_thread(
        self,
        *,
        thread_id: int,
        title: str,
        status: str,
        body: str,
    ) -> int:
        """過去サポートスレの会話をインデックスする。"""
        if not body.strip():
            return 0
        safe_title = (title or f"thread-{thread_id}").strip()
        text = (
            f"# Support: {safe_title}\n"
            f"status: {status}\n"
            f"thread_id: {thread_id}\n\n"
            f"{body}"
        )
        path = f"{SUPPORT_PATH_PREFIX}{thread_id}.md"
        return await self._index_text(path, text, "support-history")

    async def index_help_site(
        self,
        *,
        tag: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """webpage ブランチ等のヘルプ HTML / llms.txt をインデックスする。

        https://coffin299.github.io/VRCast/help/ 相当の内容を GitHub の
        ``help_ref`` から取得し、``__help__/...`` パスで保存する。
        """
        ref = (self.config.help_ref or "").strip()
        if not ref:
            return 0
        log.info("RAG help site via GitHub (%s @ %s)", self.config.repo, ref)
        # force 無しの再インデックスでも古いヘルプを残さない
        await self.store.delete_path_prefix(HELP_PATH_PREFIX)
        try:
            paths = await self._github_list_help_paths(ref)
        except EmbedError as exc:
            log.warning("help site index skipped: %s", exc)
            return 0
        if not paths:
            log.warning("help site: no matching files under ref=%s", ref)
            return 0

        total = len(paths)
        done = 0
        chunk_total = 0
        async with aiohttp.ClientSession() as session:
            for rel in paths:
                self._check_cancel()
                text = await self._github_fetch_file(session, ref, rel)
                if text is None:
                    done += 1
                    continue
                body = self._help_body_for_path(rel, text)
                if not body.strip():
                    done += 1
                    continue
                store_path = f"{HELP_PATH_PREFIX}{rel}"
                chunk_total += await self._index_text(store_path, body, tag)
                done += 1
                await self._progress(
                    on_progress, f"help {rel}", done, total
                )

        await self.store.set_meta("help_ref", ref)
        await self.store.set_meta("help_indexed_at", str(time.time()))
        log.info("RAG help site indexed: files=%s chunks=%s", total, chunk_total)
        return chunk_total

    @staticmethod
    def _help_body_for_path(rel: str, text: str) -> str:
        lower = rel.replace("\\", "/").lower()
        if lower.endswith((".html", ".htm")):
            return html_to_text(text)
        return text

    async def _github_list_help_paths(self, ref: str) -> list[str]:
        """help_include_globs に合うパスを列挙する。"""
        owner, name = self._owner_repo()
        url = f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{ref}?recursive=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._github_headers()) as resp:
                if resp.status == 404:
                    raise EmbedError(f"GitHub help ref not found: {ref}")
                if resp.status == 403:
                    raise EmbedError(
                        "GitHub API rate limited; set support.github_token "
                        "(or GITHUB_TOKEN)"
                    )
                resp.raise_for_status()
                data = await resp.json()
        if data.get("truncated"):
            log.warning("GitHub help tree truncated; some files may be missing")
        paths: list[str] = []
        for item in data.get("tree") or []:
            if item.get("type") != "blob":
                continue
            rel = str(item.get("path") or "").replace("\\", "/")
            if rel and self._help_path_included(rel):
                paths.append(rel)
        return paths

    def _help_path_included(self, rel: str) -> bool:
        posix = rel.replace("\\", "/")
        for pattern in self.config.help_include_globs:
            if self._glob_match(posix, pattern):
                return True
        return False

    async def fetch_latest_release_tag(self) -> str:
        """GitHub の最新リリースタグを返す。"""
        owner, name = self._owner_repo()
        url = f"{GITHUB_API}/repos/{owner}/{name}/releases/latest"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._github_headers()) as resp:
                if resp.status == 404:
                    return await self._fetch_latest_tag(session)
                resp.raise_for_status()
                data = await resp.json()
        return str(data.get("tag_name") or "")

    async def _fetch_latest_tag(self, session: aiohttp.ClientSession) -> str:
        owner, name = self._owner_repo()
        url = f"{GITHUB_API}/repos/{owner}/{name}/tags?per_page=1"
        async with session.get(url, headers=self._github_headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not data:
            return "HEAD"
        return str(data[0].get("name") or "HEAD")

    async def compare_and_sync(
        self,
        *,
        to_tag: str | None = None,
        repo_path: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[str, str, int]:
        """current_tag から to_tag（省略時は最新）へ diff 同期。"""
        current = await self.store.get_meta("current_tag")
        target = to_tag or await self.fetch_latest_release_tag()
        if not target:
            raise EmbedError("could not resolve target release tag")
        if not current:
            n = await self.bootstrap(
                repo_path=repo_path, tag=target, force=True, on_progress=on_progress
            )
            return ("", target, n)
        if current == target:
            # リリースは変わらなくてもヘルプサイトは別ブランチなので再取得
            help_n = await self.index_help_site(tag=target, on_progress=on_progress)
            return (current, target, help_n)
        n = await self.apply_diff(
            repo_path=repo_path,
            from_tag=current,
            to_tag=target,
            on_progress=on_progress,
        )
        n += await self.index_help_site(tag=target, on_progress=on_progress)
        return (current, target, n)

    # ------------------------------------------------------------
    # GitHub remote helpers
    # ------------------------------------------------------------
    def _github_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AshChan-RAG",
        }
        token = (
            os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or self.config.github_token
            or ""
        ).strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _github_list_paths(self, ref: str) -> list[str]:
        """指定 ref のツリーからインデックス対象パスを列挙する。"""
        owner, name = self._owner_repo()
        url = f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{ref}?recursive=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._github_headers()) as resp:
                if resp.status == 404:
                    raise EmbedError(f"GitHub ref not found: {ref}")
                if resp.status == 403:
                    raise EmbedError(
                        "GitHub API rate limited; set support.github_token "
                        "(or GITHUB_TOKEN) / local_repo_path"
                    )
                resp.raise_for_status()
                data = await resp.json()
        if data.get("truncated"):
            log.warning("GitHub tree truncated; some files may be missing")
        paths: list[str] = []
        for item in data.get("tree") or []:
            if item.get("type") != "blob":
                continue
            rel = str(item.get("path") or "").replace("\\", "/")
            if rel and self._path_included(rel):
                paths.append(rel)
        return paths

    async def _github_compare(
        self, from_tag: str, to_tag: str
    ) -> list[tuple[str, str]]:
        """Compare API で name-status 相当を返す。"""
        owner, name = self._owner_repo()
        url = f"{GITHUB_API}/repos/{owner}/{name}/compare/{from_tag}...{to_tag}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._github_headers()) as resp:
                if resp.status == 404:
                    raise EmbedError(f"GitHub compare not found: {from_tag}...{to_tag}")
                if resp.status == 403:
                    raise EmbedError(
                        "GitHub API rate limited; set support.github_token "
                        "(or GITHUB_TOKEN) / local_repo_path"
                    )
                resp.raise_for_status()
                data = await resp.json()
        changes: list[tuple[str, str]] = []
        for file in data.get("files") or []:
            status = str(file.get("status") or "")
            path = str(file.get("filename") or "").replace("\\", "/")
            if not path:
                continue
            if status == "removed":
                changes.append(("D", path))
            elif status == "renamed":
                prev = str(file.get("previous_filename") or "").replace("\\", "/")
                if prev:
                    changes.append(("D", prev))
                changes.append(("A", path))
            else:
                # added / modified / changed / copied
                changes.append(("M", path))
        return changes

    async def _github_fetch_file(
        self, session: aiohttp.ClientSession, ref: str, rel: str
    ) -> str | None:
        """raw.githubusercontent.com → Contents API の順で本文を取る。"""
        owner, name = self._owner_repo()
        raw_url = f"{RAW_BASE}/{owner}/{name}/{ref}/{rel}"
        try:
            async with session.get(raw_url, headers={"User-Agent": "AshChan-RAG"}) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    return self._decode_bytes(raw)
        except aiohttp.ClientError as exc:
            log.debug("raw fetch failed %s: %s", rel, exc)

        api_url = f"{GITHUB_API}/repos/{owner}/{name}/contents/{rel}?ref={ref}"
        try:
            async with session.get(api_url, headers=self._github_headers()) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except aiohttp.ClientError:
            return None
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not content:
            return None
        try:
            raw = base64.b64decode(content)
        except (ValueError, TypeError):
            return None
        return self._decode_bytes(raw)

    @staticmethod
    def _decode_bytes(raw: bytes) -> str | None:
        if b"\0" in raw[:8000]:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("cp932")
            except UnicodeDecodeError:
                return None

    async def _index_text(self, rel: str, text: str, tag: str) -> int:
        limited = text[: self.config.max_file_chars]
        digest = hashlib.sha256(limited.encode("utf-8", errors="ignore")).hexdigest()
        chunks = chunk_file(
            rel,
            limited,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )
        if not chunks:
            await self.store.delete_path(rel)
            return 0
        vectors = await self.embedder.embed_passages([c.text for c in chunks])
        payload = [
            (c.index, c.text, vectors[i]) for i, c in enumerate(chunks)
        ]
        return await self.store.replace_file_chunks(rel, tag, digest, payload)

    def _owner_repo(self) -> tuple[str, str]:
        parts = self.config.repo.strip().split("/")
        if len(parts) != 2:
            raise EmbedError(f"invalid repo: {self.config.repo}")
        return parts[0], parts[1]

    def _iter_files(self, root: Path) -> Iterable[tuple[str, Path]]:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if self._path_included(rel):
                yield rel, path

    def _path_included(self, rel: str) -> bool:
        posix = rel.replace("\\", "/")
        for pattern in self.config.exclude_globs:
            if self._glob_match(posix, pattern):
                return False
        for pattern in self.config.include_globs:
            if self._glob_match(posix, pattern):
                return True
        return False

    @staticmethod
    def _glob_match(posix: str, pattern: str) -> bool:
        """fnmatch で ** 付き glob をある程度扱う。"""
        if fnmatch.fnmatch(posix, pattern):
            return True
        if pattern.startswith("**/"):
            tail = pattern[3:]
            if fnmatch.fnmatch(posix, tail):
                return True
            if fnmatch.fnmatch(posix.split("/")[-1], tail):
                return True
            parts = posix.split("/")
            for i in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), tail):
                    return True
            if tail.endswith("/**"):
                fragment = tail[:-3]
                if f"/{fragment}/" in f"/{posix}/" or posix.startswith(f"{fragment}/"):
                    return True
        # 途中の **（例: help/**/*.html）
        if "/**/" in pattern:
            prefix, suffix = pattern.split("/**/", 1)
            if posix == prefix or posix.startswith(f"{prefix}/"):
                rest = posix[len(prefix) :].lstrip("/")
                if fnmatch.fnmatch(rest, suffix):
                    return True
                parts = rest.split("/") if rest else []
                for i in range(len(parts)):
                    if fnmatch.fnmatch("/".join(parts[i:]), suffix):
                        return True
                if parts and fnmatch.fnmatch(parts[-1], suffix):
                    return True
        elif pattern.endswith("/**"):
            prefix = pattern[:-3]
            if posix == prefix or posix.startswith(f"{prefix}/"):
                return True
        return False

    def _read_text(self, path: Path) -> str | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return self._decode_bytes(raw)

    @staticmethod
    def _git_name_status(
        root: Path, from_tag: str, to_tag: str
    ) -> list[tuple[str, str]]:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "diff",
                    "--name-status",
                    f"{from_tag}..{to_tag}",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise EmbedError(f"git failed: {exc}") from exc
        if proc.returncode != 0:
            raise EmbedError(proc.stderr.strip() or "git diff failed")
        changes: list[tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][0]
            if status == "R" and len(parts) >= 3:
                changes.append(("D", parts[1].replace("\\", "/")))
                changes.append(("A", parts[2].replace("\\", "/")))
            else:
                changes.append((status, parts[-1].replace("\\", "/")))
        return changes

    @staticmethod
    async def _progress(
        callback: ProgressCallback | None,
        message: str,
        done: int,
        total: int,
    ) -> None:
        if callback is None:
            return
        result = callback(message, done, total)
        if asyncio.iscoroutine(result):
            await result
