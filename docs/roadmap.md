# Roadmap — the three in concert, in increasingly powerful ways

The current repo proves the wiring: **Claude (subscription) builds a Cognee graph that an RLM
queries** — all local, $0. From here the interesting work is using the three together more deeply.

### 1. Dataset-scoped retrieval
Fix the `CHUNKS` cross-dataset leak (filter by `node_set` / per-corpus collection) so retrieval is
precise across many ingested sources. *Prereq for everything below.*

### 2. Corpus scale + the A/B that settles it
Ingest 30–50 sources into one graph and run the deferred experiment: **pure-RLM vs Cognee-guided
RLM vs paste-baseline**, measuring tokens / latency / answer quality. This is the regime where
retrieval-guided RLM should decisively win (and where a paste-baseline simply breaks). See
[findings.md](findings.md) for why one small doc didn't show it.

### 3. Depth-2 RLM over the graph
Apply the RLM's defining move — **symbolic recursion** — to the graph environment: an `rlm_query`
sub-call that itself issues `cognee.search` (`GRAPH_COMPLETION`/`INSIGHTS`), so the model
recursively traverses the knowledge graph instead of brute-forcing chunks. The graph becomes the
RLM's external environment.

### 4. Self-improving memory (`memify`)
Run Cognee's `memify` over query traces so the graph reweights edges and prunes stale nodes from
real usage — memory that gets *better* the more you query it, a property a stateless RLM lacks.

### 5. Generalize beyond transcripts
The shim + Cognee backend + RLM engine are corpus-agnostic. Point them at any source — docs, a
codebase, a notes/`knowledge.db` — via the same `ingest`/`ask` interface. The transcript app is
just the first application.

### 6. Package the shim
Extract `claude_shim.py` as a standalone pip micro-lib ("Claude subscription → OpenAI endpoint") —
it's useful to anyone wiring a Claude subscription into an OpenAI-shaped tool, well beyond Cognee.
