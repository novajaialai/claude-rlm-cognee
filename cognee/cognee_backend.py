#!/usr/bin/env python3
"""
cognee_backend.py — run by the cognee-env interpreter; the local, zero-paid-API Cognee backend.

Stack (all set via env BEFORE importing cognee, to dodge cognee's lru_cached config singletons):
  - LLM (graph extraction) -> claude_shim.py @ :8088 -> `claude -p` on Jake's OAuth subscription.
    instructor mode pinned to json_mode (empty default would be tool-calling, which the shim can't do).
  - Embeddings -> local `fastembed` (ONNX bge-small, 384-dim), fully local, no key. (Cognee's
    Ollama embedding path needs `transformers` + a HuggingFace tokenizer download; fastembed is
    self-contained, so we use it instead. One-time ~130MB ONNX model fetch, then offline.)
  - DBs -> embedded SQLite + LanceDB + ladybug, in an ISOLATED data dir (never touches ~/.cognee,
    which holds Jake's live session memory).

Subcommands:
  config                       print resolved config (no network) — cheap plumbing check
  probe                        ingest a tiny inline string + search it (fail fast / cheap)
  ingest  <vid> <txt_path>     add(text, dataset=vid) + cognify(datasets=[vid])
  retrieve <vid> <query> [k] [type]   search; prints passages after a __COGNEE_RESULT__ marker
"""
from __future__ import annotations
import asyncio, json, os, socket, subprocess, sys, time

STORE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(STORE)                        # cognee/ -> repo root
ISOLATED = os.path.join(REPO_ROOT, ".cognee-data")
SHIM_PORT = int(os.environ.get("SHIM_PORT", "8088"))
SHIM_READY = os.path.join(REPO_ROOT, ".shim-ready.json")
RESULT_MARK = "__COGNEE_RESULT__"

# --- configure cognee via env BEFORE import (most reliable) ---
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("LLM_ENDPOINT", f"http://localhost:{SHIM_PORT}/v1")
os.environ.setdefault("LLM_MODEL", "gpt-4o")              # litellm-friendly name; shim ignores it -> sonnet
os.environ.setdefault("LLM_API_KEY", "local-oauth-claude-p")
os.environ.setdefault("LLM_INSTRUCTOR_MODE", "json_mode")  # CRITICAL: avoids tool-calling default
os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
os.environ.setdefault("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
os.environ.setdefault("CACHING", "false")

os.makedirs(ISOLATED, exist_ok=True)
import cognee                                              # noqa: E402
from cognee import SearchType                              # noqa: E402

# Isolate all storage away from ~/.cognee (Jake's live memory graph).
cognee.config.system_root_directory(ISOLATED)
cognee.config.data_root_directory(os.path.join(ISOLATED, "data"))


def ensure_shim() -> None:
    """Start claude_shim.py if it isn't already listening (so the tool is self-contained)."""
    try:
        with socket.create_connection(("127.0.0.1", SHIM_PORT), timeout=1):
            return
    except OSError:
        pass
    # The shim is stdlib-only and shells out to `claude -p`; run it on system python3.
    subprocess.Popen(["python3", os.path.join(REPO_ROOT, "shim", "claude_shim.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", SHIM_PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.5)


def _result_text(r) -> str:
    """Coerce a SearchResult / chunk into plain text, defensively."""
    for attr in ("text", "content", "chunk", "payload"):
        v = getattr(r, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    if isinstance(r, dict):
        for key in ("text", "content", "chunk", "payload"):
            if isinstance(r.get(key), str):
                return r[key]
    return str(r)


def cmd_config():
    from cognee.infrastructure.llm.config import get_llm_config
    from cognee.infrastructure.databases.vector.embeddings.config import get_embedding_config
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.databases.vector.config import get_vectordb_config
    llm = get_llm_config(); emb = get_embedding_config()
    print(json.dumps({
        "system_root": ISOLATED,
        "llm": {"provider": llm.llm_provider, "endpoint": llm.llm_endpoint,
                "model": llm.llm_model, "instructor_mode": llm.llm_instructor_mode,
                "api_key_set": bool(llm.llm_api_key)},
        "embedding": {"provider": emb.embedding_provider, "model": emb.embedding_model,
                      "endpoint": emb.embedding_endpoint, "dims": emb.embedding_dimensions},
        "graph": get_graph_config().graph_database_provider,
        "vector": get_vectordb_config().vector_db_provider,
    }, indent=2))


async def cmd_probe():
    ensure_shim()
    text = ("Cognee is an open-source AI memory engine. It builds a knowledge graph and a vector "
            "index from text. Jake is testing whether his Claude subscription can power the graph "
            "extraction through a local shim, with Ollama doing the embeddings.")
    await cognee.add(text, dataset_name="probe")
    await cognee.cognify(datasets=["probe"])
    res = await cognee.search(query_text="What powers the graph extraction?",
                              query_type=SearchType.GRAPH_COMPLETION, datasets=["probe"], top_k=5)
    print(RESULT_MARK + json.dumps({"answer": [_result_text(r) for r in res]}))


async def cmd_ingest(vid: str, path: str):
    ensure_shim()
    text = open(path, encoding="utf-8").read()
    t0 = time.time()
    await cognee.add(text, dataset_name=vid)
    await cognee.cognify(datasets=[vid])
    print(RESULT_MARK + json.dumps({"vid": vid, "chars": len(text),
                                    "cognify_seconds": round(time.time() - t0, 1)}))


async def cmd_retrieve(vid: str, query: str, k: int, qtype: str):
    ensure_shim()
    st = getattr(SearchType, qtype.upper(), SearchType.CHUNKS)
    res = await cognee.search(query_text=query, query_type=st, datasets=[vid], top_k=k)
    print(RESULT_MARK + json.dumps({"type": st.value if hasattr(st, "value") else str(st),
                                    "k": k, "passages": [_result_text(r) for r in res]}))


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit("usage: cognee_backend.py config|probe|ingest <vid> <path>|retrieve <vid> <q> [k] [type]")
    cmd = a[0]
    if cmd == "config":
        cmd_config()
    elif cmd == "probe":
        asyncio.run(cmd_probe())
    elif cmd == "ingest":
        asyncio.run(cmd_ingest(a[1], a[2]))
    elif cmd == "retrieve":
        k = int(a[3]) if len(a) > 3 else 12
        qtype = a[4] if len(a) > 4 else "CHUNKS"
        asyncio.run(cmd_retrieve(a[1], a[2], k, qtype))
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
