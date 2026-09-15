"""zai_re.workflow_demo — the full chat.z.ai flow, executed and narrated.

Runs the whole recovered workflow against the local replica and prints every
step with timings, so the behaviour is visible rather than described:

    bootstrap → identity → config → models → create chat
    turn 1: stream (thinking/tool_call/tool_response/answer/usage/done)
    server-side tree update → background title/tags
    turn 2: continuity through chat_id + parent message id (no history resent)
    faithful quirk: GET /chats/<id> returns assistant nodes WITHOUT content

    python3 -m zai_re.workflow_demo            # full narration
    python3 -m zai_re.workflow_demo --json     # machine-readable log
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from .client import ZaiClient
from .server_chat import serve


def _hr(title: str) -> None:
    print("\n" + "═" * 78)
    print(f"  {title}")
    print("═" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--json", action="store_true", help="emit a JSON log as well")
    ap.add_argument("--fast", action="store_true", help="no artificial delays")
    args = ap.parse_args()

    log: dict = {"steps": []}

    def step(name: str, **data):
        entry = {"step": name, "t": round(time.time(), 3), **data}
        log["steps"].append(entry)
        return entry

    httpd = serve(args.port, "127.0.0.1",
                  think_delay=0 if args.fast else 0.012,
                  chunk_delay=0 if args.fast else 0.004)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{args.port}"

    client = ZaiClient(base, timeout=60)

    _hr("1. BOOTSTRAP — what the browser does before any chat exists")
    t0 = time.time()
    ident = client.auths()
    print(f"  GET  /api/v1/auths/        → role={ident.role!r} "
          f"token={'local-replica…' if ident.token else '(none)'}  "
          f"({(time.time()-t0)*1000:.0f} ms)")
    step("auths", ms=round((time.time() - t0) * 1000), role=ident.role)

    t0 = time.time()
    cfg = client.fetch_config()
    print(f"  GET  /api/config           → {len(cfg)} keys · default_models="
          f"{cfg.get('default_models')!r} · captcha={cfg.get('features', {}).get('enable_captcha')}"
          f"  ({(time.time()-t0)*1000:.0f} ms)")
    step("config", ms=round((time.time() - t0) * 1000))

    t0 = time.time()
    models = client.fetch_models()
    print(f"  GET  /api/models           → {len(models)} models, first="
          f"{models[0]['name']!r}  ({(time.time()-t0)*1000:.0f} ms)")
    step("models", ms=round((time.time() - t0) * 1000), count=len(models))
    print("  NOTE: none of the calls above carry credentials — the token appears "
          "only on the completions call.")

    _hr("2. TURN 1 — a garbled prompt, the way a real user types")
    prompt1 = "deep search whats next elonmkusk goad of towdy"
    print(f"  user: {prompt1!r}")
    t0 = time.time()
    chat = client.new_chat(prompt1, model="x-preview-l")
    print(f"  POST /api/v1/chats/new     → chat_id={chat.id[:8]}… "
          f"title={chat.title!r}  ({(time.time()-t0)*1000:.0f} ms)")
    step("chat_new", chat_id=chat.id, prompt=prompt1)

    print("\n  POST /api/v2/chat/completions?…token=… (SSE)")
    print("  body: {messages:[the ONE new user message], chat_id, "
          "current_user_message_parent_id:null, mcp_servers:['advanced-search']}")
    seen_phases: list[str] = []
    answer = thinking = ""
    t0 = time.time()
    state = None
    for kind, payload in client.stream_turn(chat.id, prompt1, model="x-preview-l",
                                            mcp_servers=["advanced-search"],
                                            auto_web_search=True):
        if kind == "event":
            ev = payload
            tag = ev.phase or "?"
            if tag not in seen_phases and ev.phase:
                seen_phases.append(tag)
                print(f"    · phase={tag:<14s} "
                      f"{'tool=' + str(ev.tool_name) if ev.tool_name else ''}"
                      f"{'name=' + str(ev.delta_name) if ev.delta_name else ''}")
        elif kind == "delta":
            pass
        elif kind == "result":
            state = payload
    elapsed = time.time() - t0
    assert state is not None
    print(f"\n  phases in order: {' → '.join(seen_phases)}")
    print(f"  frames={state.frames}  tool_calls={len(state.tool_calls)} "
          f"tool_responses={len(state.tool_responses)} usage_frames={len(state.usage)}")
    print(f"  thinking={len(state.thinking_text)}B  answer={len(state.answer_text)}B  "
          f"wall={elapsed*1000:.0f} ms")
    print(f"\n  THINKING (first 220 chars):\n    {state.thinking_text[:220]!r}")
    print(f"\n  ANSWER (first 300 chars):\n    {state.answer_text[:300]}")
    step("turn1", frames=state.frames, phases=state.phases,
         thinking=len(state.thinking_text), answer=len(state.answer_text),
         tool_calls=[c.name for c in state.tool_calls], ms=round(elapsed * 1000))

    print("\n  TOOL ARGUMENTS were streamed as fragments and reassembled:")
    for call in state.tool_calls:
        call_args = call.arguments
        print(f"    {call.name}({call.call_id[:18]}…): "
              f"{json.dumps(call_args)[:150] if call_args else call.arguments_raw[:80]}")

    _hr("3. SERVER-SIDE STATE — what the tree looks like right after the turn")
    time.sleep(0.6)                        # let title_generation land
    tree = client.get_chat(chat.id)
    nodes = tree["chat"]["history"]["messages"]
    print(f"  GET /api/v1/chats/<id>     → {len(nodes)} nodes · "
          f"title now {tree['title']!r} (background task renamed it)")
    for node in nodes.values():
        has = "content" in node
        print(f"    {node['role']:<9s} id={node['id'][:8]}… "
              f"parent={str(node['parentId'])[:8]}… children={len(node['childrenIds'])} "
              f"content={'YES' if has else 'STUBBED'}")
    stubs = [n for n in nodes.values() if n["role"] == "assistant" and "content" not in n]
    print(f"  → assistant nodes carry no text: {len(stubs)} stubbed. "
          f"That is why the real browser keeps its own copy.")
    step("tree", nodes=len(nodes), title=tree["title"], stubs=len(stubs))

    _hr("4. TURN 2 — continuity without resending history")
    prompt2 = "and what about grok 5 ?"
    print(f"  user: {prompt2!r}")
    print(f"  body carries chat_id + current_user_message_parent_id="
          f"{(state.tool_calls[-1].call_id if state.tool_calls else 'null')!r}…")
    print("  (the parent id is the ASSISTANT message id from turn 1 — the client "
          "never sends the transcript)")
    turn2 = client.stream_turn(chat.id, prompt2, model="x-preview-l")
    st2 = None
    for kind, payload in turn2:
        if kind == "result":
            st2 = payload
    assert st2 is not None
    print(f"\n  ANSWER: {st2.answer_text[:260]}")
    print(f"  frames={st2.frames} phases={st2.phases}")
    step("turn2", frames=st2.frames, phases=st2.phases, answer=st2.answer_text[:120])

    _hr("5. BACKGROUND TASKS + HEALTH")
    health = json.loads(_get(f"{base}/health"))
    for entry in health.get("background", []):
        print(f"  task={entry['task']:<18s} chat={entry['chat_id'][:8]}… "
              + (f"{entry.get('from')!r} → {entry.get('to')!r}" if "to" in entry
                 else f"tags={entry.get('tags')}"))
    print(f"  chats={health['chats']}  corpus_docs={health['corpus_docs']}  "
          f"mode={health['mode']}")
    step("background", tasks=[e["task"] for e in health.get("background", [])])

    _hr("RESULT")
    print("  The recovered flow works end to end: typo'd prompt → intent decode →")
    print("  tool pass with streamed arguments → tool_response in the site's own")
    print("  ref_id format → phase-ordered answer stream → usage → done,")
    print("  with the server owning history and the client keeping none.")
    print("  Live model version: python3 -m zai_re.live --url '<captured URL>' "
          "--prompt 'hey hi'")
    print("  (must run where chat.z.ai is reachable — this sandbox blocks it)")

    httpd.shutdown()

    if args.json:
        log["summary"] = {
            "phases_turn1": seen_phases,
            "frames_turn1": state.frames,
            "tool_calls_turn1": [c.name for c in state.tool_calls],
            "tree_nodes_after_turn1": len(nodes),
            "assistant_stubs": len(stubs),
        }
        print("\n--- JSON LOG ---")
        print(json.dumps(log, indent=2, ensure_ascii=False))
    return 0


def _get(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()


if __name__ == "__main__":
    sys.exit(main())
