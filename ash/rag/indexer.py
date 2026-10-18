"""リポジトリ全文 / 差分の RAG インデックス更新。"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import aiohttp

from ash.config import SupportConfig
from ash.rag.chunker import chunk_file
from ash.rag.embedder import EmbedError, Embedder
from ash.rag.store import RagStore

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], Awaitable[None] | None]


class RagJobCancelled(Exception):
    """インデックスジョブがキャンセルされた。"""


class RagIndexer:
    """フル bootstrap とタグ差分更新。"""

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

    async def bootstrap(
        self,
        *,
        repo_path: Path | None = None,
        tag: str,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """全文インデックス。force で既存チャンクを消して作り直す。"""
        root = self._resolve_repo(repo_path)
        if force:
            await self.store.clear_all()
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
        await self.store.set_meta("current_tag", tag)
        await self.store.set_meta("embed_model", self.embedder.model)
        await self.store.set_meta("indexed_at", str(time.time()))
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
        root = self._resolve_repo(repo_path)
        changes = self._git_name_status(root, from_tag, to_tag)
        if not changes:
            # タグ間に差が無くても current を進める
            await self.store.set_meta("prev_tag", from_tag)
            await self.store.set_meta("current_tag", to_tag)
            await self.store.set_meta("indexed_at", str(time.time()))
            return 0
        total = len(changes)
        done = 0
        chunk_total = 0
        for status, rel in changes:
            self._check_cancel()
            if not self._path_included(rel):
                done += 1
                continue
            if status == "D":
                await self.store.delete_path(rel)
            else:
                abs_path = root / rel
                text = self._read_text(abs_path) if abs_path.is_file() else None
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

    async def fetch_latest_release_tag(self) -> str:
        """GitHub の最新リリースタグを返す。"""
        owner, name = self._owner_repo()
        url = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"Accept": "application/vnd.github+json"}
            ) as resp:
                if resp.status == 404:
                    # リリースが無ければ tags の先頭
                    return await self._fetch_latest_tag(session)
                resp.raise_for_status()
                data = await resp.json()
        return str(data.get("tag_name") or "")

    async def _fetch_latest_tag(self, session: aiohttp.ClientSession) -> str:
        owner, name = self._owner_repo()
        url = f"https://api.github.com/repos/{owner}/{name}/tags?per_page=1"
        async with session.get(
            url, headers={"Accept": "application/vnd.github+json"}
        ) as resp:
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
            # 初回はフル bootstrap
            n = await self.bootstrap(
                repo_path=repo_path, tag=target, force=True, on_progress=on_progress
            )
            return ("", target, n)
        if current == target:
            return (current, target, 0)
        n = await self.apply_diff(
            repo_path=repo_path,
            from_tag=current,
            to_tag=target,
            on_progress=on_progress,
        )
        return (current, target, n)

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

    def _resolve_repo(self, repo_path: Path | None) -> Path:
        if repo_path is not None:
            root = repo_path
        elif self.config.local_repo_path:
            root = Path(self.config.local_repo_path)
        else:
            raise EmbedError(
                "local_repo_path is empty; set support.local_repo_path "
                "or pass --repo-path"
            )
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise EmbedError(f"repo path not found: {root}")
        return root

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
            # **/Library/** → パス断片
            if tail.endswith("/**"):
                fragment = tail[:-3]
                if f"/{fragment}/" in f"/{posix}/" or posix.startswith(f"{fragment}/"):
                    return True
        return False

    def _read_text(self, path: Path) -> str | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if b"\0" in raw[:8000]:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("cp932")
            except UnicodeDecodeError:
                return None

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
            # rename: R100\told\tnew
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
