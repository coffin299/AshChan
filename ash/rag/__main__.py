"""RAG CLI: python -m ash.rag bootstrap|diff|status"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ash.config import BASE_DIR, SupportConfig, load_config
from ash.rag.embedder import EmbedError
from ash.rag.indexer import RagIndexer, RagJobCancelled
from ash.rag.store import RagStore

DB_PATH = BASE_DIR / "data" / "ashchan.db"


def _build_support_from_env(base: SupportConfig | None = None) -> SupportConfig:
    """環境変数と任意の config から SupportConfig を組み立てる。"""
    cfg = SupportConfig() if base is None else base
    if os.environ.get("RAG_BASE_URL"):
        cfg.embedding_base_url = os.environ["RAG_BASE_URL"]
    if os.environ.get("RAG_API_KEY"):
        cfg.embedding_api_key = os.environ["RAG_API_KEY"]
    if os.environ.get("RAG_EMBED_MODEL"):
        cfg.embedding_model = os.environ["RAG_EMBED_MODEL"]
    if os.environ.get("VRCAST_REPO_PATH"):
        cfg.local_repo_path = os.environ["VRCAST_REPO_PATH"]
    if not cfg.embedding_base_url:
        cfg.embedding_base_url = "http://127.0.0.1:1234/v1"
    if not cfg.embedding_api_key:
        cfg.embedding_api_key = os.environ.get("NVIDIA_API_KEY") or "lm-studio"
    if not cfg.include_globs:
        cfg.include_globs = [
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
    if not cfg.exclude_globs:
        cfg.exclude_globs = [
            "**/Library/**",
            "**/obj/**",
            "**/bin/**",
            "**/.git/**",
            "**/docs/images/**",
            "**/*.meta",
        ]
    return cfg


async def _progress(message: str, done: int, total: int) -> None:
    print(f"[{done}/{total}] {message}", flush=True)


async def cmd_status(store: RagStore) -> int:
    await store.connect()
    try:
        print(await store.export_meta_json())
        return 0
    finally:
        await store.close()


async def cmd_bootstrap(args: argparse.Namespace, support: SupportConfig) -> int:
    if args.repo_path:
        support.local_repo_path = args.repo_path
    if args.provider == "nim":
        # Prefer NIM URL / key from env or config
        if not os.environ.get("RAG_BASE_URL"):
            support.embedding_base_url = (
                support.embedding_base_url
                or "https://integrate.api.nvidia.com/v1"
            )
        if support.embedding_base_url.startswith("http://127."):
            support.embedding_base_url = "https://integrate.api.nvidia.com/v1"
        key = os.environ.get("NVIDIA_API_KEY") or support.embedding_api_key
        if not key or key == "lm-studio":
            print("ERROR: NVIDIA_API_KEY is required for --provider nim", file=sys.stderr)
            return 1
        support.embedding_api_key = key
        if not args.model:
            support.embedding_model = "nvidia/nemotron-3-embed-1b"
    if args.model:
        support.embedding_model = args.model

    store = RagStore(Path(args.db) if args.db else DB_PATH)
    await store.connect()
    indexer = RagIndexer(store, support)
    try:
        tag = args.tag or await indexer.fetch_latest_release_tag()
        print(f"Bootstrapping tag={tag} model={support.embedding_model}")
        print(f"Repo={support.local_repo_path}")
        print(f"API={support.embedding_base_url}")
        n = await indexer.bootstrap(
            tag=tag,
            force=bool(args.force),
            on_progress=_progress,
        )
        print(f"Done. chunks={n}")
        return 0
    except (EmbedError, RagJobCancelled, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await indexer.close()
        await store.close()


async def cmd_diff(args: argparse.Namespace, support: SupportConfig) -> int:
    if args.repo_path:
        support.local_repo_path = args.repo_path
    store = RagStore(Path(args.db) if args.db else DB_PATH)
    await store.connect()
    indexer = RagIndexer(store, support)
    try:
        from_tag = args.from_tag or await store.get_meta("current_tag")
        to_tag = args.to_tag or await indexer.fetch_latest_release_tag()
        if not from_tag:
            print("ERROR: no current_tag; run bootstrap first", file=sys.stderr)
            return 1
        print(f"Diff {from_tag}..{to_tag}")
        n = await indexer.apply_diff(
            from_tag=from_tag, to_tag=to_tag, on_progress=_progress
        )
        print(f"Done. chunks_updated≈{n}")
        return 0
    except (EmbedError, RagJobCancelled, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await indexer.close()
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AshChan VRCast RAG tools")
    parser.add_argument(
        "--config",
        default="",
        help="Optional config.yaml path for support settings",
    )
    parser.add_argument("--db", default="", help="SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show RAG index status")
    p_status.set_defaults(func="status")

    p_boot = sub.add_parser("bootstrap", help="Full repository index")
    p_boot.add_argument("--repo-path", default="")
    p_boot.add_argument("--tag", default="")
    p_boot.add_argument(
        "--provider",
        choices=("local", "nim"),
        default="local",
        help="local=LM Studio defaults, nim=NVIDIA NIM",
    )
    p_boot.add_argument("--model", default="")
    p_boot.add_argument("--force", action="store_true")
    p_boot.set_defaults(func="bootstrap")

    p_diff = sub.add_parser("diff", help="Apply tag diff to index")
    p_diff.add_argument("--repo-path", default="")
    p_diff.add_argument("--from-tag", default="")
    p_diff.add_argument("--to-tag", default="")
    p_diff.set_defaults(func="diff")

    args = parser.parse_args(argv)
    support: SupportConfig | None = None
    if args.config:
        cfg = load_config(Path(args.config))
        support = cfg.support
    elif (BASE_DIR / "config.yaml").exists():
        try:
            support = load_config(BASE_DIR / "config.yaml").support
        except Exception:
            support = None
    support = _build_support_from_env(support)

    if args.func == "status":
        store = RagStore(Path(args.db) if args.db else DB_PATH)
        return asyncio.run(cmd_status(store))
    if args.func == "bootstrap":
        return asyncio.run(cmd_bootstrap(args, support))
    if args.func == "diff":
        return asyncio.run(cmd_diff(args, support))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
