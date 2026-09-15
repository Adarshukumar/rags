"""zai_re.live — run the recovered protocol against the REAL chat.z.ai.

This is the piece that "lets it answer our request" from a network that can
reach the service. The sandbox this was developed in cannot (see
docs/REVERSE-ENGINEERING-LOG.md § session 2 — TLS-layer allowlist), so this
runner is written to be executed where egress exists: your machine, a VPS, or a
colab notebook.

Three modes
-----------
1. Replay a captured URL (the simplest: you copy the completions URL from
   DevTools → Network → "Copy as cURL", paste it, and only the prompt changes):

       python3 -m zai_re.live --url "https://chat.z.ai/api/v2/chat/completions?…token=…" \
                              --prompt "hey hi"

2. Full flow from a base URL (guest identity → new chat → stream → fetch tree):

       python3 -m zai_re.live --base https://chat.z.ai --prompt "hey hi"

3. Pipe/stream API for your own app:

       from zai_re.live import LiveSession
       s = LiveSession.from_url(URL)
       for delta in s.ask("deep search what's next elon musk goals of today"):
           print(delta, end="")

Auth notes
----------
* The live call authenticates with a bearer token inside the query string (that
  is the site's own design). Pass it via --url, --token, or the ZAI_TOKEN env
  var; it is never written to disk by this module.
* `captcha_verify_param` is passed through from --captcha / ZAI_CAPTCHA if you
  have one; nothing here generates or bypasses a captcha. If the service asks
  for a fresh attestation, re-run the browser flow and copy the value.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .client import DEFAULT_MODEL, ZaiClient
from .protocol import CompletionStream, Event, iter_sse_lines


@dataclass
class LiveTurn:
    prompt: str
    thinking: str = ""
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    frames: int = 0
    seconds: float = 0.0
    phases: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "thinking": self.thinking, "answer": self.answer,
                "tool_calls": self.tool_calls, "sources": self.sources,
                "usage": self.usage, "frames": self.frames,
                "seconds": round(self.seconds, 2), "phases": self.phases,
                "error": self.error}


class LiveSession:
    """Drives the real endpoint using the recovered protocol."""

    def __init__(self, client: ZaiClient, chat_id: str = "",
                 captcha: str = "", timezone: str = "UTC",
                 user_name: str = "Guest") -> None:
        self.client = client
        self.chat_id = chat_id
        self.captcha = captcha
        self.timezone = timezone
        self.user_name = user_name
        self.parent_id: Optional[str] = None
        self.transcript: list[LiveTurn] = []

    # ------------------------------------------------------------------ ctors
    @classmethod
    def from_url(cls, url: str, *, timeout: int = 120) -> "LiveSession":
        """Parse a captured completions URL: base, token, user id, chat id, tz."""
        p = urlparse(url)
        q = parse_qs(p.query)
        base = f"{p.scheme}://{p.netloc}"
        chat_id = ""
        m = re.search(r"/c/([0-9a-f-]{36})", q.get("current_url", [""])[0])
        if m:
            chat_id = m.group(1)
        client = ZaiClient(base, timeout=timeout,
                           user_agent=q.get("user_agent", [""])[0] or None or
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
        token = q.get("token", [""])[0]
        uid = q.get("user_id", [""])[0]
        from .client import Identity
        client.identity = Identity(id=uid, email="", name="Guest", role="guest",
                                   token=token)
        return cls(client, chat_id=chat_id,
                   captcha=os.environ.get("ZAI_CAPTCHA", ""),
                   timezone=q.get("timezone", ["UTC"])[0])

    @classmethod
    def from_base(cls, base: str, *, token: str = "", timeout: int = 120) -> "LiveSession":
        """Full flow: mint/resume identity, then create a chat on first ask."""
        client = ZaiClient(base, timeout=timeout)
        if token:
            from .client import Identity
            client.identity = Identity(id="", email="", name="Guest", role="guest",
                                       token=token)
        else:
            client.auths()
        session = cls(client, captcha=os.environ.get("ZAI_CAPTCHA", ""))
        try:
            session.client.fetch_config()
        except Exception:
            pass
        return session

    # ------------------------------------------------------------------- turn
    def ensure_chat(self) -> str:
        if not self.chat_id:
            chat = self.client.new_chat("", model=DEFAULT_MODEL)
            self.chat_id = chat.id
        return self.chat_id

    def ask(self, prompt: str, *, model: str = DEFAULT_MODEL, deep_search: bool = False,
            show_thinking: bool = True, verbose: bool = False) -> Iterator[str]:
        """Stream a live turn; yields answer text deltas.

        Records a LiveTurn in `self.transcript` (thinking, tools, usage, sources).
        """
        chat_id = self.ensure_chat()
        turn = LiveTurn(prompt=prompt)
        t0 = time.time()
        state = CompletionStream()

        kw: dict[str, Any] = dict(
            model=model,
            assistant_message_id=str(uuid.uuid4()),
            parent_id=self.parent_id,
            user_message_id=None,
            auto_web_search=deep_search,
            captcha_verify_param=self.captcha,
            timezone=self.timezone,
            user_name=self.user_name,
        )
        if deep_search:
            kw["mcp_servers"] = ["advanced-search"]

        try:
            for kind, payload in self.client.stream_turn(chat_id, prompt, **kw):
                if kind != "event":
                    continue
                ev: Event = payload
                delta = state.feed(ev)
                turn.frames += 1
                turn.phases[ev.phase or "?"] = turn.phases.get(ev.phase or "?", 0) + 1
                if ev.phase == "tool_call" and ev.delta_name:
                    turn.tool_calls.append({"name": ev.delta_name,
                                            "id": ev.metadata.get("tool_call_id", "")})
                    if verbose:
                        print(f"\n[tool_call] {ev.delta_name} …", file=sys.stderr)
                if ev.phase == "tool_response":
                    turn.sources.extend(re.findall(
                        r"\[ref_id=([^†\]]+)†([^†\]]*)†([^\]]+)\]",
                        ev.delta_content or ""))
                    if verbose:
                        print(f"[tool_response] {ev.tool_name} "
                              f"{len(ev.delta_content or '')} chars", file=sys.stderr)
                if ev.usage:
                    turn.usage.append(ev.usage)
                if delta:
                    yield delta
        except Exception as exc:                       # keep the loop alive
            turn.error = f"{type(exc).__name__}: {exc}"
        finally:
            turn.thinking = state.thinking_text
            turn.answer = state.answer_text
            turn.seconds = time.time() - t0
            # the assistant id becomes the parent for the next turn
            self.parent_id = kw["assistant_message_id"]
            self.transcript.append(turn)

    def ask_full(self, prompt: str, **kw) -> LiveTurn:
        for _ in self.ask(prompt, **kw):
            pass
        return self.transcript[-1]

    def resync(self) -> dict:
        """GET /api/v1/chats/<id> — server tree (assistant content is stubbed)."""
        return self.client.get_chat(self.ensure_chat())


# --------------------------------------------------------------------------- cli
def _print_turn(turn: LiveTurn, show_thinking: bool) -> None:
    if turn.thinking and show_thinking:
        print("─" * 78)
        print("THINKING:")
        print(turn.thinking)
    print("─" * 78)
    print("ANSWER:")
    print(turn.answer)
    print("─" * 78)
    if turn.tool_calls:
        print("TOOL CALLS: " + ", ".join(t["name"] for t in turn.tool_calls))
    if turn.sources:
        print(f"SOURCES ({len(turn.sources)}):")
        for ref, title, url in turn.sources[:12]:
            print(f"   {ref:16s} {title[:60]:62s} {url}")
    if turn.usage:
        print("USAGE: " + json.dumps(turn.usage))
    print(f"frames={turn.frames} phases={turn.phases} time={turn.seconds:.1f}s"
          + (f"  ERROR={turn.error}" if turn.error else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="run the recovered protocol against live chat.z.ai")
    ap.add_argument("--url", help="captured completions URL (contains token + user_id)")
    ap.add_argument("--base", default="https://chat.z.ai", help="base URL for the full flow")
    ap.add_argument("--token", default=os.environ.get("ZAI_TOKEN", ""),
                    help="bearer token (or ZAI_TOKEN env)")
    ap.add_argument("--prompt", action="append", required=True,
                    help="prompt to send (repeat for a multi-turn conversation)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--deep-search", action="store_true",
                    help="send mcp_servers:['advanced-search'] + auto_web_search")
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--captcha", default=os.environ.get("ZAI_CAPTCHA", ""),
                    help="captcha_verify_param to forward (never generated here)")
    ap.add_argument("--save", help="write the transcript JSON here")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.url:
        session = LiveSession.from_url(args.url)
        print(f"[live] replaying captured URL: chat={session.chat_id or '(new)'} "
              f"tz={session.timezone}")
    else:
        session = LiveSession.from_base(args.base, token=args.token)
        print(f"[live] base={args.base} identity="
              f"{(session.client.identity.id if session.client.identity else '?')}")

    for prompt in args.prompt:
        print(f"\n>>> {prompt}")
        turn = session.ask_full(prompt, model=args.model, deep_search=args.deep_search,
                                verbose=args.verbose)
        _print_turn(turn, show_thinking=not args.no_thinking)

    try:
        tree = session.resync()
        msgs = tree.get("chat", {}).get("history", {}).get("messages", {})
        stubs = [m for m in msgs.values() if m.get("role") == "assistant"
                 and "content" not in m]
        print(f"\n[server tree] {len(msgs)} nodes, {len(stubs)} assistant stubs "
              f"(assistant text is not stored — recovered behaviour)")
    except Exception as exc:
        print(f"\n[server tree] unavailable: {exc}")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"chat_id": session.chat_id,
                       "turns": [t.to_dict() for t in session.transcript]},
                      fh, indent=2, ensure_ascii=False)
        print(f"[live] transcript → {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
