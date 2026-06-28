#!/usr/bin/env python3
"""
transcript_query.py — RLM-style handle over long video transcripts.

The transcript is the `context`: it lives on disk (the environment), never in the
caller's context window. You interact with it through code — peek / grep / outline /
ask — and only small slices or sub-LLM-distilled answers ever come back. The `ask`
command is a depth-1 RLM: it map-reduces the transcript through `claude -p` (the
sub-LLM, no API key) and returns ONLY the final answer.

Store layout (per video id):
  <id>.transcript.txt   cleaned, de-duplicated transcript (the context variable)
  <id>.meta.json        title / channel / duration / counts
  <id>.outline.md       cached section outline (built on first `outline`/`ask`)

Usage:
  transcript_query.py ingest  <youtube_url>
  transcript_query.py list
  transcript_query.py meta    --id <id>
  transcript_query.py peek    --id <id> [--start 0] [--len 1500]
  transcript_query.py grep    --id <id> <regex> [--window 240]
  transcript_query.py outline --id <id>
  transcript_query.py ask     --id <id> "your question"   [--chunk 6000]

Default id is the most recently ingested transcript, so --id is usually optional.
"""
from __future__ import annotations
import argparse, glob, html, json, os, re, subprocess, sys, textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed

STORE = os.path.dirname(os.path.abspath(__file__))

# RLM root/sub split: a fast, cheap model for the many fan-out reads (the
# "gpt-5-mini speed" tier — Haiku here), a stronger model for the final synthesis.
FAST_MODEL = "haiku"     # map / fan-out sub-calls
SYNTH_MODEL = "sonnet"   # reduce / synthesis
SUB_MODEL = os.environ.get("TRANSCRIPT_SUB_MODEL")  # optional override of FAST_MODEL default

# Lean worker config (the real cold-start fix). A throwaway CLAUDE_CONFIG_DIR with empty
# settings + the live credentials symlinked in means each `claude -p` sub-call fires NO
# SessionStart hooks (so no junk worker-sessions pollute knowledge.db) and loads NO MCP
# servers. Measured: cold-start ~7-11s -> ~3-5s, and concurrent calls parallelize cleanly.
#
# (A persistent warm-worker pool was prototyped and removed: `claude -p` does ALL its work
# only after the first message arrives, so there is no idle boot to pre-warm, and multiple
# concurrent `--input-format stream-json` sessions stall. Pre-warming could claw back only
# ~1.3s/call in theory. So lean + the existing batched concurrency is the right answer; see
# pool-notes.md for the full investigation.)
WORKER_CONFIG = os.path.join(STORE, ".worker-config")

def _lean_env() -> dict | None:
    try:
        os.makedirs(WORKER_CONFIG, exist_ok=True)
        with open(os.path.join(WORKER_CONFIG, "settings.json"), "w") as f:
            f.write("{}")
        src = os.path.expanduser("~/.claude/.credentials.json")
        dst = os.path.join(WORKER_CONFIG, ".credentials.json")
        if os.path.exists(src) and not (os.path.islink(dst) and os.readlink(dst) == src):
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = WORKER_CONFIG
        return env
    except Exception:
        return None


# ----------------------------- Cognee backend (optional, fully local) -------
# An opt-in graph+vector retrieval backend. `cognify` builds a knowledge graph from the
# transcript using Jake's Claude *subscription* via a local OpenAI-compatible shim over
# `claude -p` (no paid API), with fastembed local embeddings — all isolated under .cognee-data.
# OFF by default; enable with `--backend cognee` or TRANSCRIPT_BACKEND=cognee. The heavy lifting
# lives in cognee_backend.py, run by the cognee-env interpreter (keeps this tool dependency-free).
_REPO_ROOT = os.path.dirname(os.path.dirname(STORE))   # apps/transcripts/ -> repo root
COGNEE_PY = os.path.join(_REPO_ROOT, "cognee-env", "bin", "python")
COGNEE_BACKEND = os.path.join(_REPO_ROOT, "cognee", "cognee_backend.py")
RESULT_MARK = "__COGNEE_RESULT__"

def _backend(args) -> str:
    return getattr(args, "backend", None) or os.environ.get("TRANSCRIPT_BACKEND") or "claude"

def _cognee_run(*cmd: str) -> dict | None:
    """Run cognee_backend.py under the cognee-env python; return the parsed RESULT_MARK JSON."""
    if not os.path.exists(COGNEE_PY):
        print(f"[cognee] env missing at {COGNEE_PY} — falling back to claude backend.", file=sys.stderr)
        return None
    try:
        r = subprocess.run([COGNEE_PY, COGNEE_BACKEND, *cmd], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print("[cognee] backend timed out.", file=sys.stderr)
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith(RESULT_MARK):
            try:
                return json.loads(line[len(RESULT_MARK):])
            except json.JSONDecodeError:
                pass
    print(f"[cognee] no result (exit {r.returncode}); stderr tail:\n{(r.stderr or '')[-400:]}", file=sys.stderr)
    return None

def _cognee_ingest(vid: str) -> None:
    res = _cognee_run("ingest", vid, _path(vid, "transcript.txt"))
    if res:
        print(f"[cognee] graph built for {vid} in {res.get('cognify_seconds')}s")

def _cognee_retrieve(vid: str, query: str, k: int) -> list[str]:
    res = _cognee_run("retrieve", vid, query, str(k), "CHUNKS")
    return [p for p in (res or {}).get("passages", []) if isinstance(p, str) and p.strip()]


# ----------------------------- store helpers --------------------------------
def _path(vid: str, ext: str) -> str:
    return os.path.join(STORE, f"{vid}.{ext}")

def _ids() -> list[str]:
    files = glob.glob(os.path.join(STORE, "*.transcript.txt"))
    files.sort(key=os.path.getmtime, reverse=True)
    return [os.path.basename(f).replace(".transcript.txt", "") for f in files]

def _resolve(vid: str | None) -> str:
    ids = _ids()
    if vid:
        if vid not in ids:
            sys.exit(f"no transcript for id '{vid}'. ingested: {ids or '(none)'}")
        return vid
    if not ids:
        sys.exit("no transcripts ingested yet. run: transcript_query.py ingest <url>")
    return ids[0]

def _load(vid: str) -> str:
    return open(_path(vid, "transcript.txt"), encoding="utf-8").read()


# ----------------------------- sub-LLM (claude -p) --------------------------
def llm_query(prompt: str, model: str | None = None) -> str:
    """One-shot sub-LLM completion via a lean `claude -p` (no hooks/MCP). The RLM `llm_query`.

    NOTE: an optional Anthropic-API backend is written below but COMMENTED OUT so it never
    spends API tokens. To switch to it, follow the recipe in the commented block.
    """
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=300, env=_lean_env())
    except FileNotFoundError:
        sys.exit("`claude` CLI not found on PATH — required for outline/ask.")
    except subprocess.TimeoutExpired:
        return "[sub-LLM timed out]"
    return (r.stdout or r.stderr).strip()


# ============================================================================
# OPTIONAL: Anthropic-API backend (COMMENTED OUT — does NOT run, does NOT spend)
# ----------------------------------------------------------------------------
# `claude -p` is free on Jake's subscription but pays ~3-5s of CLI boot per call. The
# Anthropic API has no process boot (~1-2s/call) and real async fan-out — dramatically
# faster for the map phase. The cost: it bills your ANTHROPIC_API_KEY per token (Haiku
# $1/$5 per Mtok, Sonnet $3/$15, Opus $5/$25). Left dormant on purpose.
#
# TO ENABLE (all three steps):
#   1. `pip install anthropic`  and  `export ANTHROPIC_API_KEY=sk-ant-...`
#   2. Uncomment the `import anthropic`, `_API_MODEL`, and `_api_query` block below.
#   3. In llm_query() above, replace the body with:  return _api_query(prompt, model)
#      (or gate it:  if os.environ.get("TRANSCRIPT_BACKEND") == "api": return _api_query(...))
#   The batched/concurrent + map-reduce structure is unchanged — it just gets faster.
#
# import anthropic   # official SDK; recommended over raw HTTP
#
# # Map the tool's CLI aliases -> exact API model IDs (verified current 2026-06).
# _API_MODEL = {
#     "haiku":  "claude-haiku-4-5",
#     "sonnet": "claude-sonnet-4-6",
#     "opus":   "claude-opus-4-8",
# }
# _api_client = None
#
# def _api_query(prompt: str, model: str | None = None, max_tokens: int = 8192) -> str:
#     """One-shot sub-LLM completion via the Anthropic Messages API. Drop-in for llm_query."""
#     global _api_client
#     if _api_client is None:
#         _api_client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY from env
#     model_id = _API_MODEL.get(model or FAST_MODEL, model or "claude-haiku-4-5")
#     try:
#         resp = _api_client.messages.create(
#             model=model_id,
#             max_tokens=max_tokens,
#             messages=[{"role": "user", "content": prompt}],
#         )
#     except anthropic.APIStatusError as e:
#         return f"[api error {e.status_code}: {e.message}]"
#     except anthropic.APIConnectionError:
#         return "[api connection error]"
#     # resp.content is a list of blocks; concatenate the text blocks.
#     return "".join(b.text for b in resp.content if b.type == "text").strip()
#
# Zero-dependency alternative (no `pip install`): POST the same JSON to
#   https://api.anthropic.com/v1/messages  with headers
#   {"x-api-key": $ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
#    "content-type": "application/json"}  via urllib.request, body
#   {"model": model_id, "max_tokens": N, "messages": [{"role": "user", "content": prompt}]},
#   then read  json["content"][i]["text"]  for the text blocks.
# ============================================================================


def llm_query_batched(prompts: list[str], model: str | None = None, max_workers: int = 8) -> list[str]:
    """Concurrent sub-LLM calls — the RLM `llm_query_batched`. Order preserved.

    Each `claude -p` call is ~7s of CLI/API latency, so fanning N chunks out in
    parallel turns an N×7s sequential map into ~one round-trip. This is the speed lever.
    """
    if len(prompts) <= 1:
        return [llm_query(p, model) for p in prompts]
    results: list[str] = [""] * len(prompts)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prompts))) as ex:
        futs = {ex.submit(llm_query, p, model): i for i, p in enumerate(prompts)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    return results

def chunks(text: str, size: int) -> list[str]:
    """Split on sentence-ish boundaries near `size` chars so chunks stay coherent."""
    out, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            dot = text.rfind(". ", i + size // 2, end)
            if dot != -1:
                end = dot + 1
        out.append(text[i:end].strip())
        i = end
    return [c for c in out if c]


# ----------------------------- commands -------------------------------------
def cmd_ingest(args) -> None:
    url = args.url
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    if not m:
        sys.exit(f"could not parse a video id from: {url}")
    vid = m.group(1)
    subprocess.run(
        ["yt-dlp", "--skip-download", "--write-auto-sub", "--write-sub",
         "--sub-lang", "en.*", "--sub-format", "vtt", "--write-info-json",
         "-o", os.path.join(STORE, "%(id)s.%(ext)s"), url],
        check=True,
    )
    vtt = _path(vid, "en.vtt")
    if not os.path.exists(vtt):
        alt = sorted(glob.glob(os.path.join(STORE, f"{vid}.en*.vtt")))
        if not alt:
            sys.exit("no English captions were available for this video.")
        vtt = alt[0]
    raw = open(vtt, encoding="utf-8").read()
    lines = []
    for ln in raw.splitlines():
        if "-->" in ln or ln.strip() in ("WEBVTT", "") or ln.startswith(("Kind:", "Language:", "NOTE")):
            continue
        ln = html.unescape(re.sub(r"<[^>]+>", "", ln)).strip()
        if ln:
            lines.append(ln)
    dedup = []
    for ln in lines:
        if not dedup or dedup[-1] != ln:
            dedup.append(ln)
    final: list[str] = []
    for ln in dedup:
        if final and (ln in final[-1] or final[-1] in ln):
            if len(ln) > len(final[-1]):
                final[-1] = ln
            continue
        final.append(ln)
    transcript = re.sub(r"\s+", " ", " ".join(final)).strip()
    open(_path(vid, "transcript.txt"), "w", encoding="utf-8").write(transcript)
    info = json.load(open(_path(vid, "info.json"), encoding="utf-8"))
    meta = {"id": vid, "title": info.get("title"),
            "channel": info.get("uploader") or info.get("channel"),
            "duration_min": round((info.get("duration") or 0) / 60, 1),
            "upload_date": info.get("upload_date"), "view_count": info.get("view_count"),
            "url": info.get("webpage_url"),
            "transcript_chars": len(transcript), "transcript_words": len(transcript.split())}
    json.dump(meta, open(_path(vid, "meta.json"), "w"), indent=2)
    print(f"ingested {vid}: {meta['title']} ({meta['transcript_words']} words)")
    if _backend(args) == "cognee":
        print("[cognee] building knowledge graph (claude -p extraction + local embeddings)…")
        _cognee_ingest(vid)

def cmd_list(_args) -> None:
    for vid in _ids():
        try:
            m = json.load(open(_path(vid, "meta.json")))
            print(f"{vid}  {m.get('transcript_words'):>6}w  {m.get('duration_min')}m  {m.get('title')}")
        except FileNotFoundError:
            print(f"{vid}  (no meta)")

def cmd_meta(args) -> None:
    print(open(_path(_resolve(args.id), "meta.json")).read())

def cmd_peek(args) -> None:
    t = _load(_resolve(args.id))
    print(t[args.start: args.start + args.len])

def cmd_grep(args) -> None:
    t = _load(_resolve(args.id))
    pat = re.compile(args.pattern, re.IGNORECASE)
    hits = list(pat.finditer(t))
    if not hits:
        print(f"no matches for /{args.pattern}/")
        return
    w = args.window
    for h in hits[:40]:
        s, e = max(0, h.start() - w), min(len(t), h.end() + w)
        print(f"[@{h.start()}] …{t[s:e].strip()}…\n")
    print(f"({len(hits)} match(es))")

def _outline(vid: str, model: str | None = None, concurrency: int = 8) -> str:
    cache = _path(vid, "outline.md")
    if os.path.exists(cache):
        return open(cache, encoding="utf-8").read()
    map_model = model or SUB_MODEL or FAST_MODEL
    cs = chunks(_load(vid), 7000)
    prompts = [
        f"This is part {i+1}/{len(cs)} of a talk transcript. In 2-4 terse bullets, "
        f"list the topics/claims covered. No preamble.\n\n{c}"
        for i, c in enumerate(cs)
    ]
    notes = llm_query_batched(prompts, model=map_model, max_workers=concurrency)
    outline = llm_query(
        "Merge these per-section notes into a clean ordered outline of the talk "
        "(section headers + 1-line summaries). Be faithful, no invention.\n\n"
        + "\n\n".join(notes), model=SYNTH_MODEL)
    open(cache, "w", encoding="utf-8").write(outline)
    return outline

def cmd_outline(args) -> None:
    print(_outline(_resolve(args.id), model=args.model, concurrency=args.concurrency))

def cmd_ask(args) -> None:
    """Depth-1 RLM: map the question over chunks (fast model, concurrent), reduce to one answer.

    Only the final answer returns to the caller. `--model` sets the fan-out tier
    (default Haiku, the gpt-5-mini-speed equivalent); `--reduce-model` sets the synthesis tier.
    """
    vid = _resolve(args.id)
    q = args.question
    map_model = args.model or SUB_MODEL or FAST_MODEL
    reduce_model = args.reduce_model or SYNTH_MODEL
    cs = chunks(_load(vid), args.chunk)
    if _backend(args) == "cognee":
        # Retrieval-guided RLM: let the graph+vector index pick the relevant passages, then
        # map-reduce over just those (O(retrieved)) instead of brute-forcing every chunk (O(n)).
        retrieved = _cognee_retrieve(vid, q, getattr(args, "k", 12))
        if retrieved:
            print(f"[cognee] retrieved {len(retrieved)} passages (vs {len(cs)} full chunks)", file=sys.stderr)
            cs = retrieved
        else:
            print("[cognee] no passages retrieved — falling back to full map-reduce.", file=sys.stderr)
    prompts = [
        textwrap.dedent(f"""\
            You are extracting evidence to answer a question from one chunk of a talk transcript.
            QUESTION: {q}
            Return only quotes/facts from THIS chunk relevant to the question, terse.
            If nothing relevant, reply exactly: NONE.

            CHUNK {i+1}/{len(cs)}:
            {c}""")
        for i, c in enumerate(cs)
    ]
    raw = llm_query_batched(prompts, model=map_model, max_workers=args.concurrency)
    findings = [r.strip() for r in raw if r.strip() and r.strip().upper() != "NONE"]
    if not findings:
        print("No relevant content found in the transcript for that question.")
        return
    answer = llm_query(textwrap.dedent(f"""\
        Synthesize a single, well-organized answer to the question using ONLY these notes
        extracted from a talk transcript. Be faithful; do not invent. If the notes are
        insufficient, say so.

        QUESTION: {q}

        EXTRACTED NOTES:
        {chr(10).join(f'- {n}' for n in findings)}"""), model=reduce_model)
    print(answer)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("url")
    ing.add_argument("--backend", choices=["claude", "cognee"], default=None,
                     help="cognee = also build a local Cognee knowledge graph (claude -p extraction)")
    sub.add_parser("list")
    for name in ("meta", "peek", "grep", "outline", "ask"):
        sp = sub.add_parser(name)
        sp.add_argument("--id", default=None)
        if name == "peek":
            sp.add_argument("--start", type=int, default=0)
            sp.add_argument("--len", type=int, default=1500)
        if name == "grep":
            sp.add_argument("pattern")
            sp.add_argument("--window", type=int, default=240)
        if name in ("outline", "ask"):
            sp.add_argument("--model", default=None,
                            help=f"fan-out sub-call model (default: {FAST_MODEL}; e.g. haiku/sonnet/opus or full id)")
            sp.add_argument("--concurrency", type=int, default=8,
                            help="max parallel sub-calls (default 8)")
        if name == "ask":
            sp.add_argument("question")
            sp.add_argument("--chunk", type=int, default=6000)
            sp.add_argument("--reduce-model", dest="reduce_model", default=None,
                            help=f"final synthesis model (default: {SYNTH_MODEL})")
            sp.add_argument("--backend", choices=["claude", "cognee"], default=None,
                            help="cognee = retrieve relevant passages from the graph first (default: claude)")
            sp.add_argument("--k", type=int, default=12,
                            help="cognee backend: number of passages to retrieve (default 12)")
    args = p.parse_args()
    {"ingest": cmd_ingest, "list": cmd_list, "meta": cmd_meta,
     "peek": cmd_peek, "grep": cmd_grep, "outline": cmd_outline, "ask": cmd_ask}[args.cmd](args)


if __name__ == "__main__":
    main()
