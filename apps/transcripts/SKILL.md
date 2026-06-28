---
name: video-transcripts
description: Query long YouTube/video transcripts that live on disk WITHOUT loading them into the context window — an RLM-style handle. Use when the user asks about a video you've ingested, wants to ingest a new video by URL, or wants to ask questions / get an outline of a talk. Triggers: "what does this video say about X", "ingest this video", "summarize/outline this talk", "ask the transcript".
allowed-tools: Bash
user-invocable: true
---

# Video Transcripts (RLM-style)

Transcripts are the `context`: they live on disk (the environment), **never** in your
context window. You interact through a tool — peek / grep / outline / ask — and only
small slices or sub-LLM-distilled answers come back. This is the Recursive Language
Model idea applied to video: offload the long text, query it programmatically, recurse
with cheap sub-calls (`claude -p`), surface only the result.

**Hard rule: never `cat` / Read a `*.transcript.txt` file into context.** Always go
through the tool. A 30-min talk is ~27K chars / ~7K tokens — multiply that across a
session and the window is gone. The whole point is to keep it out.

## Tool

```
TQ=~/rlm-reference/transcripts/transcript_query.py
python3 $TQ list                       # what's ingested
python3 $TQ ingest <youtube_url>       # add a new video (yt-dlp captions → clean text)
python3 $TQ meta    [--id <id>]        # title / channel / duration / counts (tiny)
python3 $TQ peek    [--id <id>] [--start 0] [--len 1500]   # raw slice
python3 $TQ grep    [--id <id>] <regex> [--window 240]     # keyword hits + surrounding text
python3 $TQ outline [--id <id>] [--model haiku] [--concurrency 8]   # cached outline (1 pass, then free)
python3 $TQ ask     [--id <id>] "question" [--model haiku] [--reduce-model sonnet] [--concurrency 8]
```

`--id` defaults to the most recently ingested transcript, so it's usually optional.

**Speed (RLM root/sub split):** `ask`/`outline` fan the per-chunk reads out **concurrently**
(the RLM `llm_query_batched`) on a **fast model — Haiku by default** (the gpt-5-mini-speed
tier), then synthesize once on a stronger model (**Sonnet**, via `--reduce-model`). Override
the map tier with `--model haiku|sonnet|opus|<full-id>`, the fan-out width with
`--concurrency N`, or set `TRANSCRIPT_SUB_MODEL` to change the default. Use `--model sonnet`
when an answer needs more reasoning per chunk and you'll trade speed for depth.

## How to choose (cheapest first — match the RLM orchestrator discipline)

1. **Locating a fact / quote / does-he-mention-X** → `grep`. Free, no LLM, returns just the window.
2. **What's the structure / give me the gist** → `outline`. One sub-LLM pass, cached after.
3. **An interpretive or synthesis question** → `ask`. Spawns `claude -p` sub-calls per chunk
   and returns only the synthesized answer — the transcript stays on disk.
4. Only `peek` a raw slice when you need exact wording grep already pointed you to.

## Cognee backend — optional, fully local graph+vector retrieval

`--backend cognee` swaps brute-force "map over every chunk" for **retrieve-then-map**: a local
Cognee knowledge graph + vector index picks the relevant passages first, so `ask` reduces over
O(retrieved) chunks instead of O(n). Fully self-hosted, **zero paid API**:

- Graph-extraction LLM = your Claude *subscription* via a local OpenAI-compatible shim over
  `claude -p` (`claude_shim.py` @ :8088). Embeddings = local `fastembed` (ONNX). DBs = embedded
  and **isolated** in `transcripts/.cognee-data/` (never touches `~/.cognee`, your live memory).
- One-time setup: a `cognee-env/` venv with `cognee` + `fastembed`.

```
python3 $TQ ingest <url> --backend cognee          # fetch + build the graph (claude -p extraction; ~90s/transcript)
python3 $TQ ask "question" --backend cognee --k 12 # retrieve k passages from the graph, then map-reduce over them
```

Cost is the Claude **subscription rate window**, not dollars (`cognify` fires many `claude -p`
calls). Best for **repeated** queries over a **large/persistent** corpus; for a single small
transcript the default brute-force `ask` is simpler and just as good. Known caveat: CHUNKS
retrieval is not yet strictly dataset-scoped across multiple ingested videos.

Prefer `grep` to pin the spot, then a narrow `peek` — reach for `ask` only when the
question genuinely needs reading-and-reasoning across the whole talk.

## Currently ingested

- `uMvTAF280so` — **"Beyond the Prompt: Goodbye slop; welcome determinism"**, David Khourshid
  (XState/Stately), AG Grid 2026-06-26, 30 min. Thesis: *move non-determinism to the
  edges, determinism to the core* ("deterministic core, agentic shell"); build an explicit
  **model** (state machines/statecharts) instead of one-shotting prose control flow.

## Add more

`python3 ~/rlm-reference/transcripts/transcript_query.py ingest <url>` then add a bullet
above. Store files per video: `<id>.transcript.txt` (context), `<id>.meta.json`,
`<id>.outline.md` (cache). Requires `yt-dlp` (captions) and `claude` CLI (outline/ask).
