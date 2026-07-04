# Live wiring — Jake's Mac (since 2026-07-03)

This stack is DEPLOYED on the Mac, not just a demo:

```
Claude Code hooks (cognee-memory plugin)
        │ session/trace capture + per-prompt recall
        ▼
Cognee server  localhost:8011   (~/.cognee-plugin/venv, data in ~/.cognee)
        │ graph extraction (LLM_ENDPOINT)
        ▼
claude_shim    localhost:8088   (LaunchAgent com.jake.claude-shim, KeepAlive)
        │ lean `claude -p --model sonnet`
        ▼
Claude subscription (OAuth; macOS: token exported from Keychain when file copy stale)

Embeddings: local fastembed (BAAI/bge-small-en-v1.5, 384-dim) — never leave the machine.
Env source of truth: ~/.cognee-plugin/llm.env  (loaded by ~/.zshenv + LaunchAgent
com.jake.cognee-llm-env via `launchctl setenv`, so any hook-spawned server inherits it).
```

Siblings on the same box:
- **Switchboard** (`~/projects/switchboard`): `route` CLI; nightly `route eval` LaunchAgent
  `com.jake.switchboard-eval` (3:15am). Ollama (llama3.2:3b, qwen2.5-coder:7b) is tier 0.
  The shim above IS the same subscription tier Switchboard's registry drives via `claude -p`.
  Claude Code offloads bulk subtasks via the `route-subtasks` skill.
- **RLM transcripts** (`apps/transcripts` here, symlinked at `~/rlm-reference/transcripts`
  so the `video-transcripts` skill works identically on the Mac and the Linux box).

Locality: everything is local except `claude -p` itself. No Cognee cloud key or base_url is
configured anywhere on this machine (config.json is {}, api_key.json points at localhost:8011).
