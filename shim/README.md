# claude_shim — your Claude subscription as an OpenAI endpoint

A ~160-line, **stdlib-only** HTTP server that speaks the OpenAI `/v1/chat/completions` protocol and
answers each request by shelling out to **`claude -p`** — the Claude Code CLI, authenticated with
your **subscription** (OAuth token in `~/.claude/.credentials.json`), **not** a metered API key.

Point any OpenAI-compatible client at it and it runs on your Claude subscription for $0:

```bash
python3 claude_shim.py                       # serves 127.0.0.1:8088
curl -s localhost:8088/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude","messages":[{"role":"user","content":"Say hi in 3 words."}]}'
# → {"choices":[{"message":{"role":"assistant","content":"Hello there, friend"}}], ...}
```

Wire it into anything that takes an OpenAI base URL — **Cognee, litellm, LangChain, the `openai`
SDK, instructor, aider, …**:

```python
import os
os.environ["OPENAI_API_KEY"]  = "anything"           # ignored; auth is your claude -p OAuth
os.environ["OPENAI_BASE_URL"] = "http://localhost:8088/v1"
```

## Knobs (env)
- `SHIM_PORT` (default `8088`)
- `SHIM_MODEL` (default `sonnet`) — the `claude -p` model used for every request (`haiku|sonnet|opus`).

## How it works
1. Renders the OpenAI `messages` into a single prompt (system first, then turns).
2. Runs `claude -p --model $SHIM_MODEL` in a **lean** `CLAUDE_CONFIG_DIR` (no SessionStart hooks /
   no MCP — fast, and no pollution of your knowledge base), strips any ```` ```json ```` fence.
3. Returns the text as `choices[0].message.content` (and a minimal SSE stream when `stream:true`).

## Notes & caveats
- **Structured output:** clients that use `instructor` should pin **JSON / MD-JSON mode**
  (`instructor.Mode.JSON`) — instructor then enforces the schema by prompting and parses the
  content, which Claude returns cleanly. Default tool-calling mode would need a heavier shim.
- **No embeddings:** Claude has no embeddings endpoint — pair the shim with a local embedder
  (e.g. `fastembed`) for vector work.
- **Cost / terms:** free in dollars, but it spends your subscription's **rate window** (5-hour),
  and using a subscription as a programmatic backend is a gray area vs. a sanctioned API. Stay
  within your plan.
- **Local only:** binds `127.0.0.1`. The lean config dir symlinks your live credentials — keep it
  off version control.
