#!/usr/bin/env python3
"""
claude_shim.py — a tiny OpenAI-compatible HTTP endpoint backed by `claude -p`.

Cognee (via litellm) wants an OpenAI `/v1/chat/completions` endpoint. `claude -p` is a CLI on
Jake's Claude *subscription* (OAuth token in ~/.claude/.credentials.json), NOT a metered API key.
This shim bridges the two: each request is rendered to a prompt, run through a lean `claude -p`
(no SessionStart hooks), and returned in OpenAI shape. So Cognee's graph extraction runs on the
subscription — zero dollars, no API key. The only budget is the 5-hour rate window.

Run:   python3 claude_shim.py            # serves 127.0.0.1:8088
Env:   SHIM_PORT (8088), SHIM_MODEL (sonnet) — the claude -p model used for extraction.
Stdlib only; threaded so Cognee's concurrent extraction calls fan out as concurrent one-shot
`claude -p` (which parallelizes).
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SHIM_PORT", "8088"))
MODEL = os.environ.get("SHIM_MODEL", "sonnet")            # claude -p alias for the extraction LLM
REPO_ROOT = os.path.dirname(STORE)                         # shim/ -> repo root
SHIM_CONFIG = os.path.join(REPO_ROOT, ".shim-config")      # throwaway lean CLAUDE_CONFIG_DIR (repo root)
READY = os.path.join(REPO_ROOT, ".shim-ready.json")
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _lean_env() -> dict:
    """Lean CLAUDE_CONFIG_DIR (empty settings + symlinked creds) -> no hooks/MCP, fast, free."""
    try:
        os.makedirs(SHIM_CONFIG, exist_ok=True)
        with open(os.path.join(SHIM_CONFIG, "settings.json"), "w") as f:
            f.write("{}")
        src = os.path.expanduser("~/.claude/.credentials.json")
        dst = os.path.join(SHIM_CONFIG, ".credentials.json")
        if os.path.exists(src) and not (os.path.islink(dst) and os.readlink(dst) == src):
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = SHIM_CONFIG
        return env
    except Exception:
        return dict(os.environ)


def _flatten(content) -> str:
    """OpenAI content may be a string or a list of {type:text,text:...} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def _render(messages: list) -> str:
    """Render OpenAI messages into one prompt: system instructions first, then the turns."""
    sys_parts, turn_parts = [], []
    for m in messages:
        role, text = m.get("role", "user"), _flatten(m.get("content"))
        if not text.strip():
            continue
        (sys_parts if role == "system" else turn_parts).append(
            text if role == "system" else f"{role.upper()}: {text}")
    return ("\n\n".join(sys_parts) + "\n\n" + "\n\n".join(turn_parts)).strip()


def _call_claude(prompt: str, timeout: float = 300) -> str:
    cmd = ["claude", "-p", "--model", MODEL]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=timeout, env=_lean_env())
    out = (r.stdout or "").strip()
    if not out:
        out = (r.stderr or "[claude -p produced no output]").strip()
    # Many instructor modes wrap JSON in a ```json fence; strip it so json.loads succeeds upstream.
    out = FENCE.sub("", out).strip()
    return out


def _completion(text: str, stream: bool) -> dict:
    base = {"id": "chatcmpl-claudeshim", "model": "claude", "created": int(time.time())}
    if stream:
        return base  # streaming handled separately
    return {**base, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [
                {"id": "claude", "object": "model", "owned_by": "anthropic-subscription"}]})
        else:
            self._json({"status": "ok", "model": MODEL})

    def do_POST(self):
        if "chat/completions" not in self.path:
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
            prompt = _render(req.get("messages", []))
            text = _call_claude(prompt)
        except subprocess.TimeoutExpired:
            self._json({"error": {"message": "claude -p timed out", "type": "timeout"}}, 504)
            return
        except Exception as e:
            self._json({"error": {"message": str(e), "type": "shim_error"}}, 500)
            return

        if req.get("stream"):
            self._stream(text)
        else:
            self._json(_completion(text, stream=False))

    def _stream(self, text: str):
        """Minimal SSE: one content chunk + a stop chunk + [DONE]."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {"id": "chatcmpl-claudeshim", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "claude"}
        first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                      "finish_reason": None}]}
        last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        for ev in (first, last):
            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    json.dump({"pid": os.getpid(), "port": PORT, "model": MODEL}, open(READY, "w"))
    sys.stderr.write(f"claude_shim: 127.0.0.1:{PORT} -> claude -p --model {MODEL}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(READY)
        except OSError:
            pass


if __name__ == "__main__":
    main()
