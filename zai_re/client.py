"""zai_re.client — stdlib-only client for the chat.z.ai protocol.

Mirrors the browser's call sequence recovered from the HAR capture:

    GET  /api/v1/auths/                       -> guest identity + JWT
    GET  /api/config                          -> feature flags / MCP servers
    GET  /api/models                          -> model catalogue
    POST /api/v1/chats/new                    -> create chat (message tree root)
    POST /api/v2/chat/completions?…token=…    -> SSE stream  (agent turn)
    GET  /api/v1/chats/{chat_id}              -> re-sync message tree

Only the completions call is authenticated, and it authenticates by putting the
bearer token in the QUERY STRING (that is the app's design, not a choice here).
Everything else in the capture is sent unauthenticated.

The transport is injectable, so the same client code runs against
`zai_re.mock_server` for offline verification:

    from zai_re.client import ZaiClient
    c = ZaiClient("http://127.0.0.1:8801")
    chat = c.new_chat("hey hi", model="x-preview-l")
    for delta in c.stream_chat(chat.id, "hey hi"):
        print(delta, end="")

NOTE ON SCOPE: `captcha_verify_param` is an Aliyun Captcha V3 attestation the
real site obtains by solving a challenge. This client forwards whatever value
you supply and never generates or bypasses one — see docs/REVERSE-ENGINEERING-LOG.md.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .protocol import CompletionStream, Event, iter_sse_lines

DEFAULT_BASE = "https://chat.z.ai"
DEFAULT_MODEL = "x-preview-l"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
FE_VERSION = "prod-fe-1.1.95"


@dataclass
class Identity:
    id: str
    email: str
    name: str
    role: str
    token: str
    token_type: str = "Bearer"
    raw: dict = field(default_factory=dict)


@dataclass
class Chat:
    id: str
    title: str
    raw: dict = field(default_factory=dict)


class ZaiClient:
    """Minimal re-implementation of the browser's chat.z.ai API usage."""

    def __init__(self, base_url: str = DEFAULT_BASE, *, timeout: int = 30,
                 user_agent: str = DEFAULT_UA, region: str = "overseas",
                 device_id: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.region = region
        self.device_id = device_id or f"uid_{uuid.uuid4().hex[:16]}"
        self.identity: Optional[Identity] = None
        self.config: dict = {}
        self.models: list[dict] = []

    # ------------------------------------------------------------------ http
    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {
            "accept": "application/json",
            "accept-language": "en-US",
            "content-type": "application/json",
            "origin": self.base_url,
            "user-agent": self.user_agent,
            "x-region": self.region,
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, *, body: Any = None,
                 query: Optional[dict] = None, headers: Optional[dict] = None,
                 stream: bool = False):
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(headers))
        resp = urllib.request.urlopen(req, timeout=self.timeout)
        if stream:
            return resp
        raw = resp.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    # ------------------------------------------------------------ handshake
    def auths(self) -> Identity:
        """GET /api/v1/auths/ — on a fresh visit this mints a guest account."""
        data = self._request("GET", "/api/v1/auths/")
        self.identity = Identity(
            id=data.get("id", ""), email=data.get("email", ""),
            name=data.get("name", ""), role=data.get("role", ""),
            token=data.get("token", ""), token_type=data.get("token_type", "Bearer"),
            raw=data,
        )
        return self.identity

    def fetch_config(self) -> dict:
        self.config = self._request("GET", "/api/config") or {}
        return self.config

    def fetch_models(self) -> list[dict]:
        data = self._request("GET", "/api/models") or {}
        self.models = data.get("data", data if isinstance(data, list) else [])
        return self.models

    # ----------------------------------------------------------------- chats
    def new_chat(self, prompt: str, *, model: str = DEFAULT_MODEL,
                 enable_thinking: bool = True, reasoning_effort: str = "max",
                 chat_id: str = "", title: str = "New Chat") -> Chat:
        """POST /api/v1/chats/new — seeds the server-side message tree."""
        msg_id = str(uuid.uuid4())
        payload = {"chat": {
            "id": chat_id, "title": title, "models": [model], "params": {},
            "history": {"messages": {msg_id: {
                "id": msg_id, "parentId": None, "childrenIds": [],
                "role": "user", "content": prompt,
                "timestamp": int(time.time()), "models": [model]}},
                "currentId": msg_id},
            "tags": [], "flags": [],
            "features": [{"server": "tool_selector_h", "status": "hidden",
                          "type": "tool_selector"}],
            "mcp_servers": [], "enable_thinking": enable_thinking,
            "reasoning_effort": reasoning_effort, "auto_web_search": False,
            "message_version": 1, "extra": {},
            "timestamp": int(time.time() * 1000), "type": "default",
        }}
        data = self._request("POST", "/api/v1/chats/new", body=payload)
        self._last_user_message_id = msg_id
        return Chat(id=data.get("id", ""), title=data.get("title", ""), raw=data)

    def get_chat(self, chat_id: str) -> dict:
        return self._request("GET", f"/api/v1/chats/{chat_id}")

    # ------------------------------------------------------------ completion
    def build_completion_request(self, chat_id: str, prompt: str, *,
                                 model: str = DEFAULT_MODEL,
                                 assistant_message_id: Optional[str] = None,
                                 parent_id: Optional[str] = None,
                                 user_message_id: Optional[str] = None,
                                 mcp_servers: Optional[list[str]] = None,
                                 auto_web_search: bool = False,
                                 enable_thinking: bool = True,
                                 reasoning_effort: str = "max",
                                 captcha_verify_param: str = "",
                                 timezone: str = "UTC",
                                 user_name: str = "Guest") -> tuple[str, dict, dict]:
        """Returns (path, query, body) exactly as the browser shapes them."""
        now = time.time()
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        msg_id = user_message_id or getattr(self, "_last_user_message_id", None) \
            or str(uuid.uuid4())
        body = {
            "stream": True,
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "signature_prompt": prompt,
            "params": {}, "extra": {},
            "features": {
                "image_generation": False, "web_search": False,
                "auto_web_search": auto_web_search, "preview_mode": True,
                "flags": [], "vlm_tools_enable": False,
                "vlm_web_search_enable": False, "vlm_website_mode": False,
                "enable_thinking": enable_thinking,
                "reasoning_effort": reasoning_effort,
            },
            "variables": {
                "{{USER_NAME}}": user_name,
                "{{USER_LOCATION}}": "Unknown",
                "{{CURRENT_DATETIME}}": local,
                "{{CURRENT_DATE}}": time.strftime("%Y-%m-%d", time.localtime(now)),
                "{{CURRENT_TIME}}": time.strftime("%H:%M:%S", time.localtime(now)),
                "{{CURRENT_WEEKDAY}}": time.strftime("%A", time.localtime(now)),
                "{{CURRENT_TIMEZONE}}": timezone,
                "{{USER_LANGUAGE}}": "en-US",
            },
            "chat_id": chat_id,
            "id": assistant_message_id or str(uuid.uuid4()),
            "current_user_message_id": msg_id,
            "current_user_message_parent_id": parent_id,
            "background_tasks": {"title_generation": True, "tags_generation": True},
        }
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        if captcha_verify_param:
            # forwarded verbatim; never synthesized here
            body["captcha_verify_param"] = captcha_verify_param

        token = self.identity.token if self.identity else ""
        query = {
            "timestamp": str(int(now * 1000)),
            "requestId": str(uuid.uuid4()),
            "user_id": self.identity.id if self.identity else "",
            "version": "0.0.1",
            "platform": "web",
            "token": token,
            "user_agent": self.user_agent,
            "language": "en-US",
            "languages": "en-US,en",
            "timezone": timezone,
            "cookie_enabled": "true",
            "screen_width": "1366", "screen_height": "768",
            "screen_resolution": "1366x768",
            "viewport_height": "712", "viewport_width": "955",
            "viewport_size": "955x712", "color_depth": "24",
            "pixel_ratio": "1",
            "current_url": f"{self.base_url}/c/{chat_id}",
            "pathname": f"/c/{chat_id}",
            "host": urllib.parse.urlparse(self.base_url).netloc,
            "hostname": urllib.parse.urlparse(self.base_url).netloc,
            "protocol": "https:",
            "title": "Z.ai - Advanced AI Chatbot",
            "timezone_offset": "0",
            "local_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now)),
            "utc_time": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now)),
            "is_mobile": "false", "is_touch": "false", "max_touch_points": "0",
            "browser_name": "Chrome", "os_name": "Windows",
            "signature_timestamp": str(int(now * 1000)),
        }
        return "/api/v2/chat/completions", query, body

    def stream_chat(self, chat_id: str, prompt: str, **kw) -> Iterator[str]:
        """Stream a turn, yielding text deltas (thinking + answer interleaved).

        Use `stream_turn()` when you want the full structured result.
        """
        for kind, payload in self.stream_turn(chat_id, prompt, **kw):
            if kind == "delta":
                yield payload

    def stream_turn(self, chat_id: str, prompt: str, **kw) -> Iterator[tuple[str, Any]]:
        """Stream a turn yielding ("delta"|"event"|"result", payload)."""
        path, query, body = self.build_completion_request(chat_id, prompt, **kw)
        resp = self._request(
            "POST", path, body=body, query=query, stream=True,
            headers={"accept": "*/*", "x-device-id": self.device_id,
                     "x-fe-version": FE_VERSION,
                     "x-signature": uuid.uuid4().hex + uuid.uuid4().hex[:32]},
        )
        state = CompletionStream()
        stream = resp
        try:
            for obj in iter_sse_lines(_decode_iter(stream)):
                ev = Event.from_wire(obj)
                delta = state.feed(ev)
                yield ("event", ev)
                if delta:
                    yield ("delta", delta)
        finally:
            try:
                stream.close()
            except Exception:
                pass
        yield ("result", state)


def _decode_iter(resp) -> Iterator[str]:
    """Yield decoded SSE lines from an http.client response object.

    Line-oriented reading is enough here: the capture shows every frame is
    newline-terminated, and the frame assembler handles frames split across
    reads anyway.
    """
    while True:
        chunk = resp.readline()
        if not chunk:
            break
        yield chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
