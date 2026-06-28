# Honest findings

The stack works — and the smoke test surfaced exactly the tradeoff worth being honest about.

## It runs, fully local, $0
On one 30-minute talk transcript (~27K chars):

| Step | Result |
|---|---|
| `cognify` (build the graph via `claude -p` + fastembed) | **93.7s**, no rate-limit hit |
| Graph extracted on the Claude *subscription* | ✓ (entities + relations, instructor `json_mode`) |
| Embeddings | local `fastembed` (no key) |
| API charges | **$0** (`ANTHROPIC_/OPENAI_API_KEY` unset) |
| Data footprint | `cognee-env` ~840 MB · `.cognee-data` ~4 MB |
| Isolation | `~/.cognee` (live session memory) untouched |

## The tradeoff: graph retrieval doesn't pay off on *one small* document

Same question, same transcript:

| | chunks processed | time | answer |
|---|---|---|---|
| default (brute-force RLM map over all chunks) | 5 | **~26s** | excellent |
| `--backend cognee` (retrieve-then-map) | **3** retrieved | ~37s | excellent, equivalent |

The Cognee path retrieved fewer chunks but was **slower** — the retrieve step (spinning the
cognee-env, importing cognee, vector search ≈ 10–15s) costs more than it saves by dropping 2
chunks, and answer quality was a wash.

## Why — and when it flips
This is the same **amortization law** that shows up everywhere in this lineage (warm process
pools, prompt caches, search indexes): **building/maintaining an index only pays when it's reused
at scale.** The graph wins when both hold:

1. **Large corpus** — retrieval prunes a *big* candidate set (hundreds of chunks → a handful),
   so the map phase shrinks dramatically. On 5 chunks there's nothing to prune.
2. **Repeated queries** — the one-time `cognify` cost amortizes across many `ask`s.

For a single small transcript queried once, the brute-force RLM is simpler and just as good. The
graph's payoff is a **corpus-scale** experiment — the next step on the [roadmap](roadmap.md).

## Known limitation
`CHUNKS` retrieval is not yet strictly dataset-scoped: with multiple datasets ingested it can
return cross-dataset passages (the map step filters irrelevant ones, so answers stay correct, but
it's wasteful). Scoping it is roadmap item #1.
