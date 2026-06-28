#!/usr/bin/env bash
# setup.sh — stand up the local cognee-env for claude-rlm-cognee.
#
# Fully local, zero paid API: Cognee (graph+vector) + fastembed (embeddings) + the claude_shim
# (your Claude *subscription* as the extraction LLM, via `claude -p`). No OpenAI/Anthropic API key.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer uv (the Cognee plugin ships one); fall back to stdlib venv + pip.
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.cognee-plugin/uv/uv" ] && UV="$HOME/.cognee-plugin/uv/uv"

echo "==> creating cognee-env (Python 3.12) + installing cognee, fastembed…"
if [ -n "$UV" ]; then
  "$UV" venv "$ROOT/cognee-env" --python 3.12
  "$UV" pip install --python "$ROOT/cognee-env/bin/python" cognee fastembed
else
  python3 -m venv "$ROOT/cognee-env"
  "$ROOT/cognee-env/bin/pip" install -U pip cognee fastembed
fi

echo "==> sanity checks:"
if command -v claude >/dev/null; then
  echo "  claude CLI: $(claude --version 2>/dev/null | head -1)  (subscription LLM for the shim ✓)"
else
  echo "  ⚠ claude CLI not found — needed for the shim (the extraction LLM runs on your Claude subscription)."
fi
"$ROOT/cognee-env/bin/python" -c "import cognee, fastembed; print('  cognee + fastembed import OK ✓')"

cat <<EOF

Done. The stack is local and free — the only budget is your Claude subscription's rate window.

Transcript demo:
  python3 apps/transcripts/transcript_query.py ingest <youtube-url> --backend cognee
  python3 apps/transcripts/transcript_query.py ask "your question" --backend cognee

The shim auto-starts on demand; or run it yourself:
  python3 shim/claude_shim.py        # serves 127.0.0.1:8088
EOF
