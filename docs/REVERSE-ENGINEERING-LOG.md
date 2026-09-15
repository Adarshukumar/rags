# Reverse-engineering log — chat.z.ai protocol

**Target:** `chat.z.ai` (Z.ai chat app, GLM-5.3-Flash preview)
**Specimen:** `chat.z.ai.har` — 55,988,600 bytes on disk, HAR 1.2, Chrome 153 DevTools export
**Capture window:** 2026-09-15 16:15:49 → 16:33:06 UTC · 1,324 entries · ~24.2 MB transferred
**Work session:** 2026-09-15 (this workspace, branch `arena/01a0a5f2-rags`)
**Deliverable:** `zai_re/` (client + replay server + verifier), `docs/zai-protocol-spec.md`, this log

---

## 0. Environment recon (and what it rules out)

| Probe | Result | Consequence |
|---|---|---|
| `python3 -V` | 3.11.2 | stdlib-only is achievable — chosen deliberately |
| `pip3 install fastapi uvicorn` | blocked by PEP 668 (externally managed env) | avoided a venv dependency trap; stayed stdlib |
| `docker --version` | **not installed** | Dockerfiles are *provided but not built*; documented as untested |
| `curl https://chat.z.ai/` | `000` in 30 ms (TCP fails) | **no live testing** from this sandbox |
| `getent hosts chat.z.ai` | resolves (155.102.56.12) | egress filtering, not DNS |
| `curl https://api.github.com`, `pypi.org` | `200` | sandbox allowlists some hosts; z.ai is not one of them |

**Decision:** verify by **replaying the captured bytes** through a local server rather than by calling the
live service. This is arguably the stronger evidence: the test input is the original wire data, not a
re-collection whose fidelity depends on the network.

**What I deliberately did not do** (and why):

* Did not attempt to reach chat.z.ai through any proxy/tunnel to "make the live test work" — the sandbox
  block is a boundary, not an obstacle to route around.
* Did not solve, replay, or synthesize the Aliyun Captcha V3 attestation. It is documented as a protocol
  field that the client must *carry*, never forge.
* Did not embed the captured bearer token anywhere new. `har_source.identity_payload()` scrubs it
  (`<scrubbed-captured-token>`, `<scrubbed-user-id>`) so every generated artifact is credential-free.

## 1. Triage — what is in the capture at all

Tools: `python3` for structural passes, `jq` for quick slicing, plus `tools/har_explore.py` (written in the
earlier session) for repeats.

```
entries 1324 · methods {GET:1163, POST:160, OPTIONS:1}
statuses {200:384, 204:58, 302:21, 304:3, 403:805, 404:1, 0:52}
hosts: icon.z.ai 805 · sdata.chatglm.cn 103 · zcode.z.ai 103 · alb.reddit.com 52 · RUM 52
       … z-cdn.chatglm.cn 28 (18.3 MB) · chat.z.ai 14
```

First-party API surface is tiny — 14 requests total:

```
GET  /api/v1/auths/                       (guest identity + JWT)
GET  /api/config                          (feature flags, MCP servers)
GET  /api/models                          (~288 KB catalogue)
GET  /api/v1/scene-cfg/  (+?model=<id>)   (~192 KB scene config)
GET  /api/v1/users/user/settings
POST /api/v1/users/user/settings/update
POST /api/v1/chats/new
POST /api/v2/chat/completions             ×3   ← the interesting target
GET  /api/v1/chats/<uuid>                 ×2
```

## 2. Reading the stream — hypotheses and how each was settled

| Hypothesis | Method | Verdict |
|---|---|---|
| Payloads are base64 in the HAR (so naive `jq` sees garbage) | compared `content.encoding` per entry | **Confirmed.** Entries 285 and 355 are `encoding: "base64"`; 237 is not. Wrote a decoder; every later step uses it. |
| The stream is SSE with a single event type | parsed every `data:` line of all 3 streams | **Confirmed.** 39 / 21 / 504 frames, all `type: "chat:completion"`. |
| Phases are a fixed vocabulary | histogram over `data.phase` | **Confirmed.** `thinking`, `answer`, `tool_call`, `tool_response`, `other`, `done`. |
| `done:true` is the only terminator | looked for early termination conditions | **Confirmed — and this is a trap.** In turn 1 the final `"."` arrives *after* the `usage` frame; a client that stops at `usage` silently truncates. Encoded as a unit test. |
| Tool arguments arrive in one frame | concatenated `delta_arguments` per `tool_call_id` | **Refuted.** Arguments are fragmented; also the first fragment carries `tool_call_id` and later ones only `{"type":"function"}`. Reassembly logic written + tested. |
| The client resends conversation history | compared `messages[]` against the previous turn | **Refuted.** Only the newest user message is sent; history lives server-side, addressed by `chat_id` + `current_user_message_parent_id`. |
| Auth is a header or a cookie | scanned all 1,324 request headers + `cookies` objects | **Refuted.** Zero cookies anywhere; the bearer token rides in the **query string**, and only on completions. |
| `signature_prompt` differs from the message text sometimes | compared all 3 turns | **Refuted.** It is always byte-identical to `messages[0].content` — a client-side copy for server-side signing/telemetry. |
| Assistant answers are stored server-side | `GET /api/v1/chats/<id>` after each turn | **Refuted.** Assistant nodes return as stubs (no `content`); text exists only in the stream. Explains the browser's re-fetch. |

## 3. The agent turn (deep search), decomposed

504 frames, 125 s, 4 tool calls, 42 distinct sources. Re-ran the segmentation and printed every call:

```
search  {"search_query":[{"q":"Elon Musk next plans 2026","recency":14},
                         {"q":"Elon Musk announcement today","recency":7},
                         {"q":"Elon Musk latest news September 2026","recency":14}]}
search  {"search_query":[{"q":"Elon Musk xAI Grok announcement","recency":7},
                         {"q":"Elon Musk Starship Mars launch 2026","recency":30},
                         {"q":"Elon Musk news September 14 15 16 2026","recency":7}]}
search  {"search_query":[{"q":"Elon Musk first trillionaire SpaceX IPO June 2026","recency":120},
                         {"q":"SpaceXAI Grok 5 launch date AGI","recency":60},
                         {"q":"Tesla robotaxi Optimus 2026 update","recency":30}]}
open    {"open":[{"ref_id":"turn0search12","lineno":1},{"ref_id":"turn0search10","lineno":1}]}
          → tool_response: "Ref id turn0search12 is invalid / Ref id turn0search10 is invalid"  (64 B)
          → model proceeds, cites them anyway
```

Two protocol-level findings fall out of this: search results are injected as text blocks shaped
`[ref_id=turnNsearchM†Title†url]\nDate: …\n<snippet>` (which the UI turns into source cards via the
`【turnNsearchM】` citation markers in the answer), and the invalid-`open` path has **no retry logic** —
a real, reproducible agent weakness rather than a guess about model quality.

## 4. Building the verification harness

Because live calls were off the table, the test had to be self-contained:

```
HAR bytes ──► har_source.py ──► mock_server.py ──► client.py ──► CompletionStream
   (specimen)     (extract)        (replay)          (recovered)      (state machine)
                     └──────────────────────────────────► verify.py compares
                                                        against the same HAR bytes
```

Design choices and why:

* **Replay, not paraphrase.** `mock_server` reproduces the original bytes (chunked, frame-aligned,
  `text/event-stream`, `x-trace-id`), so the client meets the real wire format including mid-frame splits
  (`--chunk 64` used in verification).
* **Speed control.** `--speed N` / `--instant` — the 125 s deep-search turn replays in milliseconds for CI
  and 8× for human viewing.
* **Synthetic fallback.** Prompts absent from the HAR get a generated stream in the identical shape, proving
  the client is not merely coupled to three canned byte strings.
* **Zero dependencies.** No fastapi/uvicorn/httpx — the whole harness runs on 3.11 stdlib, so it cannot rot.

## 5. Verification results (executed, not asserted)

`python3 -m unittest discover -s tests -t .` → **10/10 PASS** (parser noise tolerance, split-frame
reassembly, thinking/answer separation, the usage-mid-answer trap, tool-call reassembly, tool responses,
har-ground-truth shape, credential scrubbing).

`python3 -m zai_re.verify` → **full parity across 3 captured turns**:

| turn | prompt | frames | phases | thinking | answer | parity |
|---|---|---|---|---|---|---|
| 1 | `hey hi` | 39 | thinking 29 · answer 8 · usage 1 · done 1 | 726 B | 144 B | 8/8 checks PASS |
| 2 | `whats ur name ?` | 21 | thinking 9 · answer 10 · usage 1 · done 1 | 194 B | 220 B | 8/8 checks PASS |
| 3 | `deep search …` | 504 | thinking 291 · tool_call 31 · tool_response 4 · answer 173 · usage 4 · done 1 | 5,324 B | 3,280 B | 8/8 checks PASS |

Checks per turn: thinking bytes · answer bytes · frame count · phase histogram · tool_calls (name, id,
arguments_raw) · tool_responses · usage frames · done flag — all exact equality against a stream parsed
straight from the HAR.

Plus: usage-before-final-delta handled (tail `" topics you'd like to discuss."` retained) and the synthetic
fallback passed (76 frames matching the real shape).

Live smoke test against chat.z.ai: **not possible from this workspace** (see §0) — recorded as a known gap,
not worked around.

## 6. Runtime state

The replay server was also run as a long-lived process for manual inspection:

```
python3 -m zai_re.mock_server --port 8801 --host 0.0.0.0 --speed 8 --chunk 96
GET /health → {"ok":true,"streams":3,"prompts":["hey hi","whats ur name ?","deep search …"]}
GET /        → demo UI (live frame counters, thinking pane, answer pane, raw protocol log)
```

## 7. Files produced

```
zai_re/
├── protocol.py      wire format: SSE reader (batch + incremental), Event, CompletionStream, ToolCall
├── client.py        ZaiClient: auths → config → models → new_chat → stream_turn (stdlib urllib)
├── har_source.py    HAR → ground truth (streams, payloads, scrubbed identity)
├── mock_server.py   SSE replay server + demo UI (routes mirror the real site)
├── verify.py        parity harness (mock + client vs captured bytes)
├── Dockerfile       stdlib image for the replay server  (untested — no docker here)
├── docker-compose.yml
└── __init__.py
tests/
└── test_protocol.py 10 unit tests incl. the two protocol traps
docs/
├── zai-protocol-spec.md   the specification
└── REVERSE-ENGINEERING-LOG.md   this file
```

## 8. Reproduce

```bash
python3 -m unittest discover -s tests -t .     # 10 tests
python3 -m zai_re.verify                       # parity report, exit 0 = parity
python3 -m zai_re.mock_server --port 8801 --speed 8   # demo UI at /
```

## 9. Open questions / next steps

1. `x-signature` — is it an HMAC over the body, or a random nonce? Only a live session can decide; the
   capture shows a fresh 64-hex value per call with `signature_timestamp == timestamp`.
2. Does the server enforce `captcha_verify_param` on every completion, or only on suspicious clients?
   (Three of three completions carried one.)
3. `POST /api/v1/chats/new` and `GET /api/v1/chats/<id>` return user-scoped data with **no credential** in
   the request — whether that is scoped by IP/session server-side is unresolved from a single capture.
4. The `open` tool's ref-id namespace (`turn0searchN`) suggests per-turn indexing; a second turn's refs would
   confirm whether the prefix (`turn0`, `turn1`, …) increments per agent turn.
5. If a live session is ever run from a permitted network: capture one turn with `--header` logging to test
   whether the 32 fingerprint query params are validated or decorative.
