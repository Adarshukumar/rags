#!/usr/bin/env python3
"""
har_explore.py - inspect a browser HAR file (1.2) without dumping 50 MB of JSON.

Usage:
    python3 tools/har_explore.py summary    [har]
    python3 tools/har_explore.py apis       [har]
    python3 tools/har_explore.py transcript [har]  # -> stdout, redirect to a file
    python3 tools/har_explore.py issues     [har]
    python3 tools/har_explore.py timing     [har]

Default HAR path: chat.z.ai.har
Handles base64-encoded response bodies (Chrome exports them for binary/br payloads).
"""

from __future__ import annotations

import base64
import collections
import json
import re
import sys
import urllib.parse

DEFAULT_HAR = "chat.z.ai.har"


def load(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)["log"]["entries"]


def body(entry: dict) -> str:
    """Response body as text, transparently decoding base64 payloads."""
    content = entry["response"].get("content", {}) or {}
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", "replace")
        except Exception:
            return ""
    return text


def req_body(entry: dict) -> str:
    return (entry["request"].get("postData", {}) or {}).get("text", "") or ""


def sse_events(entry: dict) -> list[dict]:
    """Parse an SSE response body into the list of JSON payloads."""
    out = []
    for line in body(entry).splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return out


def ms(value) -> str:
    return f"{value:.0f}" if isinstance(value, (int, float)) and value >= 0 else "-"


# --------------------------------------------------------------------------- summary
def cmd_summary(entries, args):
    log_hosts = collections.Counter()
    statuses = collections.Counter()
    methods = collections.Counter()
    mimes = collections.Counter()
    sizes = collections.Counter()
    for e in entries:
        host = urllib.parse.urlparse(e["request"]["url"]).netloc
        log_hosts[host] += 1
        statuses[e["response"]["status"]] += 1
        methods[e["request"]["method"]] += 1
        mimes[(e["response"]["content"].get("mimeType") or "?").split(";")[0]] += 1
        sizes[host] += e["response"].get("_transferSize") or e["response"]["content"].get("size") or 0

    print(f"entries        : {len(entries)}")
    print(f"window         : {entries[0]['startedDateTime']} -> {entries[-1]['startedDateTime']}")
    print(f"methods        : {dict(methods)}")
    print(f"statuses       : {dict(sorted(statuses.items()))}")
    print(f"mimeTypes      : {dict(mimes.most_common(12))}")
    print(f"total transfer : {sum(sizes.values()):,} bytes")
    print("\ntop hosts by request count:")
    for host, n in log_hosts.most_common(15):
        print(f"  {n:5d}  {sizes[host]:>12,} B  {host}")
    print("\nfull host list:")
    for host, n in log_hosts.most_common():
        print(f"  {n:5d}  {sizes[host]:>12,} B  {host}")


# --------------------------------------------------------------------------- apis
def cmd_apis(entries, args):
    print("first-party API calls (chat.z.ai):\n")
    for i, e in enumerate(entries):
        url = e["request"]["url"]
        if "chat.z.ai" not in url or "/api/" not in url:
            continue
        path = urllib.parse.urlparse(url).path
        print(f"[{i:4d}] {e['request']['method']:5s} {path:48s} "
              f"{e['response']['status']} {e['response']['content'].get('mimeType')} "
              f"{e['time']:>9.0f} ms")
        rb = req_body(e)
        if rb:
            print(f"        req body  : {rb[:400]}{'...' if len(rb) > 400 else ''}")


# --------------------------------------------------------------------------- transcript
def _tool_calls(events) -> list[dict]:
    """Re-assemble streamed tool_call argument fragments."""
    calls, cur = [], None
    for ev in events:
        data = ev.get("data", {}) or {}
        if data.get("phase") != "tool_call":
            continue
        if data.get("delta_name"):
            cur = {"name": data["delta_name"], "args": "",
                   "id": (data.get("metadata") or {}).get("tool_call_id")}
            calls.append(cur)
        if data.get("delta_arguments") and cur is not None:
            cur["args"] += data["delta_arguments"]
    for c in calls:
        try:
            c["parsed"] = json.loads(c["args"])
        except Exception:
            c["parsed"] = None
    return calls


def cmd_transcript(entries, args):
    for i, e in enumerate(entries):
        url = e["request"]["url"]
        path = urllib.parse.urlparse(url).path
        if path == "/api/v1/chats/new":
            req = json.loads(req_body(e))
            msgs = req["chat"]["history"]["messages"]
            first = next(iter(msgs.values()))
            print("#" * 78)
            print(f"# CHAT CREATED  models={req['chat'].get('models')} "
                  f"thinking={req['chat'].get('enable_thinking')} "
                  f"effort={req['chat'].get('reasoning_effort')}")
            print(f"# first user message: {first['content']!r}")
            print("#" * 78)
            continue
        if path != "/api/v2/chat/completions":
            continue

        req = json.loads(req_body(e))
        events = sse_events(e)
        print("\n" + "=" * 78)
        print(f"TURN  (entry #{i})  model={req.get('model')}  "
              f"mcp={req.get('mcp_servers')}  web_search={req['features'].get('web_search')} "
              f"auto={req['features'].get('auto_web_search')}")
        print(f"wall time: {e['time']:.0f} ms | stream events: {len(events)}")
        for m in req.get("messages", []):
            print(f"USER: {m.get('content')}")
        print("-" * 78)
        for c in _tool_calls(events):
            print(f"TOOL CALL {c['name']} ({c['id']}): "
                  f"{json.dumps(c['parsed'], ensure_ascii=False)}")

        buf: list[str] = []
        phase: str | None = None

        def flush():
            nonlocal phase, buf
            if buf:
                print(f"{phase.upper()}: {''.join(buf)}\n")
                buf, phase = [], None

        for ev in events:
            data = ev.get("data", {}) or {}
            p = data.get("phase")
            if p in ("thinking", "answer") and data.get("delta_content") is not None:
                if p != phase:
                    flush()
                phase = p
                buf.append(data["delta_content"])
            elif p == "tool_response":
                flush()
                print(f"TOOL RESPONSE [{data.get('tool_name')}] status={data.get('status')} "
                      f"({len(data.get('delta_content', ''))} chars)")
                print("  " + (data.get("delta_content", "")[:600].replace("\n", "\n  ")))
                print()
            elif p == "tool_call":
                if data.get("delta_name"):
                    flush()
            elif p == "other" and data.get("usage"):
                print(f"USAGE: {json.dumps(data['usage'])}")
            elif data.get("done"):
                flush()
                print("[stream done]\n")
        flush()


# --------------------------------------------------------------------------- issues
def cmd_issues(entries, args):
    print("### failing / failed requests\n")
    by_url = collections.Counter()
    for e in entries:
        st = e["response"]["status"]
        if st >= 400 or st == 0:
            by_url[(urllib.parse.urlparse(e["request"]["url"]).netloc +
                    urllib.parse.urlparse(e["request"]["url"]).path, st)] += 1
    for (url, st), n in by_url.most_common(40):
        print(f"  {n:5d} x status {st:<4} {url[:110]}")

    print("\n### duplicate fetches of the same URL (>= 10x)\n")
    counts = collections.Counter(e["request"]["url"].split("?")[0] for e in entries)
    for url, n in counts.most_common(15):
        if n >= 10:
            print(f"  {n:5d} x {url[:120]}")

    print("\n### slowest responses\n")
    for i, e in sorted(enumerate(entries), key=lambda x: -x[1]["time"])[:12]:
        print(f"  [{i:4d}] {e['time']:>9.0f} ms  {e['request']['method']:5s} "
              f"{e['request']['url'].split('?')[0][:90]} -> {e['response']['status']}")

    print("\n### error text from server / app logs in payloads\n")
    pat = re.compile(r"(denied by [^\"\\]*|global_network_error|Failed to fetch|"
                     r"Ref id \S+ is invalid|\"level\":\"error\"|Invalid [A-Za-z ]+)")
    for i, e in enumerate(entries):
        blob = body(e)[:20000] + req_body(e)[:20000]
        for hit in set(pat.findall(blob)):
            print(f"  [{i:4d}] {hit[:110]}")


# --------------------------------------------------------------------------- timing
def cmd_timing(entries, args):
    print("### phase timings, sum over all entries\n")
    total = collections.Counter()
    for e in entries:
        for k in ("blocked", "dns", "connect", "ssl", "send", "wait", "receive"):
            total[k] += max(e["timings"].get(k, 0) or 0, 0)
    print("  " + "  ".join(f"{k}={v/1000:.1f}s" for k, v in total.items()))

    print("\n### per-host blocked (queueing) time\n")
    per = collections.Counter()
    for e in entries:
        per[urllib.parse.urlparse(e["request"]["url"]).netloc] += \
            max(e["timings"].get("blocked", 0) or 0, 0)
    for host, t in per.most_common(12):
        print(f"  {t/1000:>9.1f} s  {host}")

    print("\n### request start distribution (per minute)\n")
    per_min = collections.Counter(e["startedDateTime"][:16] for e in entries)
    for minute in sorted(per_min):
        print(f"  {minute}  {per_min[minute]:4d}  {'#' * min(per_min[minute] // 4, 120)}")


COMMANDS = {
    "summary": cmd_summary,
    "apis": cmd_apis,
    "transcript": cmd_transcript,
    "issues": cmd_issues,
    "timing": cmd_timing,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HAR
    COMMANDS[sys.argv[1]](load(path), sys.argv[2:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
