# claude-rlm-cognee

**Claude + RLM + Cognee, in concert** — a local-first memory & reasoning stack powered by your
**Claude subscription**. No paid API key, no cloud, no usage-metered tokens.

Three ideas, wired together so each makes the others stronger:

| Thread | Role | How it runs here |
|---|---|---|
| **Claude** | the reasoning | Your *subscription* via `claude -p` (OAuth), exposed to any tool as an OpenAI endpoint by a tiny shim |
| **RLM** ([Recursive Language Models](https://github.com/alexzhang13/rlm)) | the context discipline | Don't stuff long inputs into the window — offload them and query via code + cheap sub-calls |
| **Cognee** ([topoteretes/cognee](https://github.com/topoteretes/cognee)) | the memory | A self-hosted knowledge **graph + vector** index built from your content |

The keystone is the shim: it lets Cognee's graph-building LLM **be your Claude subscription**
instead of a metered OpenAI/Anthropic key. So the whole stack — graph extraction, embeddings,
storage — runs on your machine for **$0**, bounded only by your Claude rate window.

## Architecture

```
                      ┌─────────────────────────────────────────────┐
  any OpenAI client ─▶│ shim/claude_shim.py  (127.0.0.1:8088)        │
  (Cognee, litellm,   │   renders the request → `claude -p` (OAuth)  │──▶ your Claude subscription
   LangChain, …)      │   → returns OpenAI-shaped JSON               │      (no API key)
                      └─────────────────────────────────────────────┘
        Cognee uses it for entity/relationship extraction ──┐
                                                             ▼
  ingest ─▶ cognee/cognee_backend.py ─▶ knowledge GRAPH (ladybug) + VECTOR (LanceDB) + SQLite
                 embeddings = local fastembed (ONNX)          (all embedded, isolated, on disk)
                                                             ▲
  ask ─▶ apps/transcripts/transcript_query.py ──────────────┘
         RLM map-reduce, but RETRIEVE-guided: the graph picks the relevant passages first,
         so it reduces over O(retrieved) chunks instead of brute-forcing O(n).
```

## What's here

| Path | What it is |
|---|---|
| **`shim/claude_shim.py`** | The keystone — *use your Claude subscription as an OpenAI-compatible endpoint.* Stdlib-only, reusable far beyond this repo. See [`shim/README.md`](shim/README.md). |
| **`cognee/cognee_backend.py`** | Self-hosted Cognee configured to run on the shim + fastembed, with isolated local DBs. |
| **`apps/transcripts/`** | Flagship demo: an RLM transcript tool with an opt-in `--backend cognee` retrieve-then-map path. |
| **`docs/`** | [architecture](docs/architecture.md) · [honest findings](docs/findings.md) · [roadmap](docs/roadmap.md) |

## Quickstart

Needs: the `claude` CLI (logged into your subscription) and Python 3.12. `yt-dlp` for the demo.

```bash
./setup.sh                                   # creates cognee-env, installs cognee + fastembed

cd apps/transcripts
python3 transcript_query.py ingest "https://www.youtube.com/watch?v=<id>" --backend cognee
python3 transcript_query.py ask "what's the core argument?" --backend cognee
```

`ingest --backend cognee` builds a knowledge graph from the transcript using `claude -p` for
extraction (~90s/transcript). `ask --backend cognee` retrieves the relevant passages from the
graph, then RLM-reduces over just those.

## Cost & constraints (honest)

- **$0 in API charges.** The extraction LLM is your Claude subscription; embeddings are local
  (`fastembed`); storage is embedded. The only budget is the subscription's **5-hour rate window**
  — `cognify` fires many `claude -p` calls, so large corpora can hit it (throttle / spread out).
- **CPU-only** (no GPU assumed): builds are slower and a bit lower-fidelity than a cloud model.
- A Claude *subscription* via the shim is a gray area vs. a sanctioned API — it's rate-limited
  accordingly. Use within your plan.
- **When does the graph actually help?** Not on one small document — see
  [docs/findings.md](docs/findings.md). It pays off for **repeated queries over a large corpus**.

## Status

Working stack, smoke-tested end to end (graph built on the Claude subscription, retrieval-guided
`ask` verified). This is the foundation for using the three in increasingly powerful ways — see
the [roadmap](docs/roadmap.md). The measurement-study sibling lives at
[novajaialai/rlm-in-practice](https://github.com/novajaialai/rlm-in-practice).

## License
MIT © 2026 Jacob Dart. Cognee and fastembed are Apache-2.0 dependencies (installed, not vendored);
RLM is MIT. See [LICENSE](LICENSE).
