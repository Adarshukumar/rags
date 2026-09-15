"""zai_re.mock_server — a faithful local stand-in for chat.z.ai.

Serves the same routes, the same JSON shapes and the same SSE byte format as
the captured site, replaying the real captured streams for the three prompts in
the HAR and synthesizing well-formed streams for anything else.

Why this exists: the sandbox cannot reach chat.z.ai (egress filtered), and a
replay server is the honest way to verify that the recovered client works — it
consumes the *original bytes*, not a summary of them.

Run:
    python3 -m zai_re.mock_server --port 8801
    python3 -m zai_re.mock_server --port 8801 --speed 8     # 8x faster replay

Endpoints
    GET  /                        demo UI (streams a replayed turn in-browser)
    GET  /health                  {"ok": true, "streams": n, ...}
    GET  /api/v1/auths/           guest identity (the captured one, scrubbed)
    GET  /api/config              captured feature flags / MCP list
    GET  /api/models              captured model catalogue
    GET  /api/v1/scene-cfg/       small stub (the real one is ~192 KB of prompts)
    POST /api/v1/chats/new        creates a message tree, echoes ids back
    GET  /api/v1/chats/<id>       returns the stored tree
    POST /api/v2/chat/completions replays/synthesizes the token stream
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .har_source import HarSource
from .protocol import EVENT_TYPE

DEMO_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>zai_re — protocol replay</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
      background:#0d1117;color:#e6edf3}
 header{padding:14px 18px;border-bottom:1px solid #21262d;display:flex;gap:14px;
        align-items:baseline;flex-wrap:wrap}
 h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
 .muted{color:#7d8590;font-size:12px}
 main{display:grid;grid-template-columns:1fr 1fr;gap:0;height:calc(100vh - 58px)}
 section{padding:16px 18px;overflow:auto}
 section+section{border-left:1px solid #21262d}
 .row{display:flex;gap:8px;margin-bottom:10px}
 textarea{flex:1;background:#161b22;color:#e6edf3;border:1px solid #30363d;
          border-radius:6px;padding:9px;font:inherit;resize:vertical;min-height:60px}
 button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 14px;
        font:inherit;cursor:pointer}
 button.alt{background:#21262d;border:1px solid #30363d}
 button:disabled{opacity:.5;cursor:default}
 .stat{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 14px;font-size:12px;color:#7d8590}
 .stat b{color:#e6edf3;font-weight:600}
 .pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
       border:1px solid #30363d;margin-right:5px}
 .thinking{color:#8b949e;white-space:pre-wrap;border-left:2px solid #30363d;
           padding-left:10px;margin-bottom:12px;font-size:12.5px}
 .answer{white-space:pre-wrap}
 .answer h1,.answer h2,.answer h3{font-size:14px;margin:14px 0 6px;color:#79c0ff}
 .answer code{background:#161b22;padding:1px 4px;border-radius:4px}
 pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px;
     overflow:auto}
 #log{font-size:12px;color:#7d8590;white-space:pre-wrap}
</style></head><body>
<header>
  <h1>zai_re · chat.z.ai protocol replay</h1>
  <span class="muted">mock server · consumes the captured HAR bytes</span>
</header>
<main>
  <section>
    <div class="row"><textarea id="prompt">deep search whats next elonmkusk goad of towdy</textarea></div>
    <div class="row">
      <button id="send">Stream turn</button>
      <button id="quick" class="alt">hey hi</button>
      <button id="quick2" class="alt">whats ur name ?</button>
    </div>
    <div class="stat" id="stat"></div>
    <div class="thinking" id="thinking"></div>
    <div class="answer" id="answer"></div>
  </section>
  <section>
    <div class="stat">raw protocol frames <b id="frames">0</b></div>
    <div id="log"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let ctrl = null;
async function run(prompt){
  if (ctrl) ctrl.abort();
  ctrl = new AbortController();
  $('thinking').textContent = ''; $('answer').textContent = ''; $('log').textContent='';
  $('frames').textContent = '0';
  $('send').disabled = true;
  const t0 = performance.now();
  let frames = 0, counts = {}, answer = '', thinking = '';
  try {
    const r = await fetch('/api/v2/chat/completions?platform=web', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({stream:true, model:'x-preview-l',
        messages:[{role:'user',content:prompt}], signature_prompt:prompt,
        chat_id:'demo'}),
      signal: ctrl.signal });
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      let nl;
      while ((nl = buf.indexOf('\\n')) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl+1);
        if (!line.startsWith('data:')) continue;
        const ev = JSON.parse(line.slice(5).trim()); frames++;
        const d = ev.data || {}, ph = d.phase || (d.done ? 'done' : '?');
        counts[ph] = (counts[ph]||0)+1;
        if (ph === 'thinking' && d.delta_content) { thinking += d.delta_content;
          $('thinking').textContent = thinking; }
        if (ph === 'answer' && d.delta_content) { answer += d.delta_content;
          $('answer').textContent = answer; }
        if (ph === 'tool_call') $('log').textContent +=
          `tool_call ${d.delta_name||''} ${d.delta_arguments||''}\\n`;
        if (ph === 'tool_response') $('log').textContent +=
          `tool_response ${d.tool_name} ${(d.delta_content||'').length} chars\\n`;
        if (d.usage) $('log').textContent += `usage ${JSON.stringify(d.usage)}\\n`;
        $('frames').textContent = frames;
        const ms = Math.round(performance.now()-t0);
        $('stat').innerHTML = Object.entries(counts).map(([k,v]) =>
          `<span class="pill">${k} <b>${v}</b></span>`).join('') +
          ` <span>· ${ms} ms</span>`;
      }
    }
    $('log').textContent += '\\n[stream closed]';
  } catch(e) { $('log').textContent += '\\n[error] ' + e.message; }
  $('send').disabled = false;
}
$('send').onclick = () => run($('prompt').value);
$('quick').onclick = () => { $('prompt').value='hey hi'; run('hey hi'); };
$('quick2').onclick = () => { $('prompt').value='whats ur name ?'; run('whats ur name ?'); };
</script></body></html>
"""


class MockState:
    """Holds the replay corpus and the synthetic chat store."""

    def __init__(self, har_path: Optional[str] = None, speed: float = 1.0,
                 chunk: int = 120, instant: bool = False) -> None:
        self.source = HarSource(har_path) if har_path else HarSource()
        self.speed = max(speed, 0.01)
        self.chunk = max(chunk, 1)
        self.instant = instant
        self.streams = self.source.streams()
        self.by_prompt = {s.prompt.strip().lower(): s for s in self.streams}
        self.chats: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------- synthetic
    def synth_stream(self, prompt: str, model: str) -> str:
        """A well-formed stream for prompts the HAR never saw."""
        answer = (f"[mock] No captured stream matches {prompt!r}, so this turn is "
                  f"synthesized in the same wire format by {model}.\n\n"
                  "Frames: thinking -> answer -> usage -> done.")
        thinking = "The prompt was not in the capture; emit a synthetic reply."
        out = []

        def frame(**data):
            out.append("data: " + json.dumps(
                {"type": EVENT_TYPE, "data": data}, ensure_ascii=False) + "\n\n")

        for word in _words(thinking, 3):
            frame(delta_content=word, phase="thinking")
        for word in _words(answer, 3):
            frame(delta_content=word, phase="answer")
        frame(phase="other", usage={"prompt_tokens": 42, "completion_tokens": 64,
                                    "total_tokens": 106,
                                    "prompt_tokens_details": {}})
        frame(phase="done", done=True)
        return "".join(out)


def _words(text: str, n: int):
    for i in range(0, len(text), n):
        yield text[i:i + n]


# --------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: MockState = None  # type: ignore

    # -- helpers ------------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console quiet-ish
        if "--verbose" in __import__("sys").argv:
            super().log_message(fmt, *args)

    def _json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        st = self.state
        if path == "/":
            body = DEMO_PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            self._json({"ok": True, "streams": len(st.streams),
                        "prompts": [s.prompt for s in st.streams],
                        "speed": st.speed, "chats": len(st.chats)})
        elif path == "/api/v1/auths/":
            self._json(st.source.identity_payload())
        elif path == "/api/config":
            self._json(st.source.config_payload())
        elif path == "/api/models":
            self._json(st.source.models_payload())
        elif path.startswith("/api/v1/scene-cfg/"):
            self._json({"code": 0, "msg": "success",
                        "data": [{"namespace": "zai", "model": "default",
                                  "scene": "mock", "options": {}}]})
        elif path.startswith("/api/v1/chats/"):
            cid = path.rsplit("/", 1)[-1]
            chat = st.chats.get(cid)
            if chat is None:
                self._json({"detail": "Chat not found"}, 404)
            else:
                self._json(chat)
        else:
            self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        st = self.state
        if path == "/api/v1/chats/new":
            body = self._read_json()
            chat = body.get("chat", {})
            cid = str(uuid.uuid4())
            chat_id = chat.get("id") or cid
            history = chat.get("history", {})
            record = {
                "id": chat_id, "user_id": "mock-user", "title": chat.get("title", "New Chat"),
                "chat": {**chat, "id": chat_id}, "updated_at": int(time.time()),
                "created_at": int(time.time()), "share_id": None, "archived": False,
                "pinned": False, "meta": {"auto_web_search": False, "flags": None,
                                          "mcp_servers": [], "models": chat.get("models", []),
                                          "workspace_id": chat_id},
                "folder_id": None, "message_version": 1, "type": chat.get("type", "default"),
                "im_context": None,
            }
            with st.lock:
                st.chats[chat_id] = record
            self._json(record)
        elif path == "/api/v2/chat/completions":
            self._stream_completions()
        else:
            self._json({"detail": "Not Found"}, 404)

    # -- the interesting one -------------------------------------------------
    def _stream_completions(self):
        body = self._read_json()
        prompt = (body.get("signature_prompt")
                  or next((m.get("content", "") for m in body.get("messages", [])
                           if m.get("role") == "user"), ""))
        model = body.get("model", "x-preview-l")
        st = self.state
        captured = st.by_prompt.get(prompt.strip().lower())
        raw = captured.raw_sse if captured else st.synth_stream(prompt, model)

        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-trace-id", uuid.uuid4().hex[:16])
        self.send_header("access-control-allow-origin", "*")
        # chunked, like the real thing
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        try:
            for piece in _slice_frames(raw, st.chunk):
                self._write_chunk(piece)
                if not st.instant:
                    # keep the shape of the original: ~40 frames/s
                    time.sleep(min(0.02 / st.speed, 0.2))
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()


def _slice_frames(raw: str, size: int):
    """Split the SSE text into chunk-sized pieces on frame boundaries."""
    frames = re.findall(r"data: .*?\n\n", raw, flags=re.S)
    buf = ""
    for f in frames:
        buf += f
        while len(buf) >= size:
            yield buf[:size].encode()
            buf = buf[size:]
    if buf:
        yield buf.encode()


def serve(port: int = 8801, host: str = "0.0.0.0", speed: float = 1.0,
          har: Optional[str] = None, instant: bool = False, chunk: int = 120,
          ready: Optional[threading.Event] = None) -> ThreadingHTTPServer:
    state = MockState(har_path=har, speed=speed, chunk=chunk, instant=instant)
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    if ready is not None:
        ready.set()
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="chat.z.ai protocol replay server")
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--instant", action="store_true", help="no inter-frame delay")
    ap.add_argument("--chunk", type=int, default=120, help="SSE chunk size in bytes")
    ap.add_argument("--har", default=None, help="path to the HAR (default: repo root)")
    args = ap.parse_args()

    httpd = serve(args.port, args.host, args.speed, args.har, args.instant, args.chunk)
    st: MockState = httpd.RequestHandlerClass.state  # type: ignore[attr-defined]
    print(f"[zai_re] replay server on http://{args.host}:{args.port}")
    print(f"[zai_re] {len(st.streams)} captured streams: "
          f"{[s.prompt[:38] for s in st.streams]}")
    print(f"[zai_re] demo UI: /  · health: /health  · speed: {args.speed}x")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[zai_re] bye")


if __name__ == "__main__":
    main()
