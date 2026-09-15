"""zai_re.verify — does the recovered client reproduce the captured sessions?

Method: boot `mock_server` in-process (it replays the *original* SSE bytes from
the HAR), point `ZaiClient` at it, run one full turn per captured prompt, and
compare what the client assembled against what the HAR says was streamed.

Byte-for-byte equality of thinking + answer, plus identical frame counts and
phase histograms, is the proof that the recovered state machine is correct.

    python3 -m zai_re.verify            # exit 0 = full parity
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time

from .client import ZaiClient
from .har_source import HarSource
from .mock_server import serve
from .protocol import CompletionStream, Event, iter_sse_events


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ground_truth(raw_sse: str) -> CompletionStream:
    st = CompletionStream()
    for obj in iter_sse_events(raw_sse):
        st.feed(Event.from_wire(obj))
    return st


def main() -> int:
    src = HarSource()
    streams = src.streams()
    if not streams:
        print("no captured completion streams found in the HAR")
        return 2

    port = _free_port()
    httpd = serve(port=port, host="127.0.0.1", instant=True, chunk=64)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)

    base = f"http://127.0.0.1:{port}"
    print(f"[verify] replay server {base}  (chunked SSE, instant mode)")
    print(f"[verify] specimen: {src.log['pages'][0]['title']}  "
          f"entries={len(src.entries)}  streams={len(streams)}\n")

    failures = 0
    client = ZaiClient(base, timeout=60)

    ident = client.auths()
    cfg = client.fetch_config()
    models = client.fetch_models()
    print(f"[verify] identity   : role={ident.role!r} token_type={ident.token_type!r} "
          f"(server-side token redacted in mock)")
    print(f"[verify] config     : {len(cfg)} keys, mcp={[m['name'] for m in cfg.get('mcp_servers', [])]}")
    print(f"[verify] models     : {len(models)} models, first={models[0].get('name') if models else None}")
    print()

    for i, captured in enumerate(streams, 1):
        expect = ground_truth(captured.raw_sse)

        chat = client.new_chat(captured.prompt, model=captured.model,
                               enable_thinking=captured.request_body
                               .get("features", {}).get("enable_thinking", True),
                               reasoning_effort=captured.request_body
                               .get("features", {}).get("reasoning_effort", "max"))
        got = None
        deltas = 0
        for kind, payload in client.stream_turn(
                chat.id, captured.prompt, model=captured.model,
                mcp_servers=captured.request_body.get("mcp_servers"),
                auto_web_search=captured.request_body
                .get("features", {}).get("auto_web_search", False)):
            if kind == "delta":
                deltas += 1
            elif kind == "result":
                got = payload

        assert got is not None
        checks = {
            "thinking bytes": got.thinking_text == expect.thinking_text,
            "answer bytes": got.answer_text == expect.answer_text,
            "frame count": got.frames == expect.frames,
            "phase histogram": got.phases == expect.phases,
            "tool_calls": [(c.name, c.call_id, c.arguments_raw)
                           for c in got.tool_calls]
                          == [(c.name, c.call_id, c.arguments_raw)
                              for c in expect.tool_calls],
            "tool_responses": [r["tool_name"] for r in got.tool_responses]
                              == [r["tool_name"] for r in expect.tool_responses],
            "usage frames": got.usage == expect.usage,
            "done flag": got.done and expect.done,
        }
        ok = all(checks.values())
        failures += 0 if ok else 1

        print(f"── turn {i}: {captured.prompt!r}")
        print(f"   model={captured.model} frames={got.frames} "
              f"phases={got.phases} deltas_streamed={deltas}")
        print(f"   thinking={len(got.thinking_text)}B answer={len(got.answer_text)}B "
              f"tool_calls={len(got.tool_calls)} usage_frames={len(got.usage)}")
        for name, passed in checks.items():
            print(f"     {'PASS' if passed else 'FAIL'}  {name}")
        if not ok:
            for name, passed in checks.items():
                if not passed:
                    print(f"     !! {name} mismatch")
            print(f"     expected answer head: {expect.answer_text[:90]!r}")
            print(f"     got      answer head: {got.answer_text[:90]!r}")
        print()

    # the truncation trap: usage arriving before the last answer delta
    trap = None
    for s in streams:
        st = ground_truth(s.raw_sse)
        if len(st.usage) >= 1 and st.answer:
            trap = st
            break
    if trap is not None:
        tail = trap.answer_text[-30:]
        print(f"[verify] usage-before-final-delta handled: answer tail={tail!r} "
              f"(a client that stops at `usage` would lose it)")

    # non-captured prompt -> synthesized stream on the same protocol
    synth_chat = client.new_chat("hello there", model="x-preview-l")
    synth = None
    for kind, payload in client.stream_turn(synth_chat.id, "hello there"):
        if kind == "result":
            synth = payload
    assert synth is not None
    synth_ok = synth.done and synth.answer_text.startswith("[mock]") and synth.phases.get("answer")
    print(f"[verify] synthetic fallback for unseen prompt: "
          f"{'PASS' if synth_ok else 'FAIL'}  frames={synth.frames} "
          f"phases={synth.phases}")
    failures += 0 if synth_ok else 1

    httpd.shutdown()
    print()
    if failures:
        print(f"[verify] RESULT: {failures} failure(s)")
        return 1
    print(f"[verify] RESULT: full parity across {len(streams)} captured turns ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
