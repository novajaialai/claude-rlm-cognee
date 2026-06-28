# Architecture

## Data flow

```
INGEST
  transcript text
    └─▶ cognee.add(dataset=<id>)  ─▶  cognee.cognify(datasets=[<id>])
            classify → chunk → extract_graph_and_summarize → add_data_points
                                      │                          │
                                      ▼                          ▼
                          LLM (entities/relations,        embeddings (fastembed,
                          via the shim → claude -p)        local ONNX, 384-dim)
                                      │                          │
                                      ▼                          ▼
                          GRAPH (ladybug)              VECTOR (LanceDB)   + SQLite (relational)
                                       all under  .cognee-data/  (isolated, on disk)

QUERY (ask --backend cognee)
  question
    └─▶ cognee.search(CHUNKS, dataset=<id>, top_k=k)   ─▶  relevant passages
            └─▶ RLM map (claude -p, haiku) over those passages
                    └─▶ RLM reduce (claude -p, sonnet)  ─▶  answer
```

## The three components

- **`shim/claude_shim.py`** — OpenAI `/v1/chat/completions` over `claude -p`. Lets Cognee's
  extraction LLM be the Claude *subscription* (OAuth), not a metered key. Stdlib-only, threaded so
  Cognee's concurrent extraction calls fan out as concurrent one-shot `claude -p`.

- **`cognee/cognee_backend.py`** — configures Cognee (via env, *before* import, to dodge its
  lru-cached config singletons) and exposes `config` / `probe` / `ingest` / `retrieve`. Key config:
  - LLM → `provider=openai`, `endpoint=http://localhost:8088/v1`, **`LLM_INSTRUCTOR_MODE=json_mode`**
    (the empty default is tool-calling, which the shim can't do — this is the make-or-break knob).
  - Embeddings → `provider=fastembed`, `BAAI/bge-small-en-v1.5`, 384-dim (Cognee's Ollama path needs
    `transformers` + a HF tokenizer; fastembed is self-contained).
  - Storage → `system_root_directory(.cognee-data)` so nothing touches `~/.cognee` (the user's live
    Cognee session memory). Defaults: SQLite + LanceDB + ladybug, all embedded.

- **`apps/transcripts/transcript_query.py`** — the RLM transcript tool. `--backend cognee` adds a
  retrieve-before-map step: `cs = _cognee_retrieve(...)` replaces the full chunk list, so the
  existing map-reduce runs over fewer, pre-filtered chunks. Off by default; the default `claude`
  backend is the brute-force RLM.

## Layout & paths
Each script computes a `REPO_ROOT` and points runtime artifacts there:
`cognee-env/` (the venv), `.cognee-data/` (the stores), `.shim-config/` + `.shim-ready.json`
(the shim's lean config — gitignored; symlinks live credentials). The three scripts call each
other by repo-relative path (`transcript_query → cognee_backend → claude_shim`); the shim
auto-starts on demand (liveness is checked by port).

## Why nothing leaks money
The only LLM calls go through `claude -p` (subscription OAuth); embeddings + storage are local.
Verified: `cognify` completes with `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` unset.
