# How chat.z.ai streams a real answer — the workflow, end to end

This is the companion to `zai-protocol-spec.md` (which describes the *frames*).
This document describes the *machine*: who holds what state, what happens between
a keystroke and a streamed token, how the tool pass works, and how continuity
survives a reload.

Evidence base: the 1,324-entry HAR capture, plus `zai_re/` which re-implements
the workflow and reproduces it (39 tests and a parity harness, all passing).
Where behaviour is enforced by the local replica it is marked **[verified]**,
and where a claim is inferred it is marked **[inferred]**.

---

## 1. The cast

| Actor | Role | Evidence in capture |
|---|---|---|
| Browser SPA (`prod-fe-1.1.95`) | owns the UI, the SSE reader, the captcha attestation, and a *mirror* of the transcript | 18.3 MB JS bundle; `x-fe-version` header on every completion |
| Edge CDN (`Server: ESA`, Alibaba) | terminates TLS, compresses (br), chunks the stream, sets CORS | `via: ens-cache*.sg25`, `x-site-cache-status: DYNAMIC` |
| Chat API | creates chats, **owns the canonical message tree**, stitches turns, runs background jobs | `/api/v1/chats/new`, `GET /chats/<id>`, "New Chat" → "Greeting Query" rename |
| Model service (`x-preview-l` = GLM-5.3-Flash) | produces `thinking`, decisions to call tools, and the `answer` | 504-frame deep-search stream |
| Tool/MCP service (`advanced-search`) | `search` (9 queries) and `open` (2 refs) | `mcp_servers:["advanced-search"]` in the request |
| Aliyun Captcha + cloudauth | per-turn attestation + device fingerprint | re-solved before every completion |
| Telemetry | first-party `e.gif`/`blank.gif`, Aliyun RUM, GA4/Ads, Clarity, Bing, Reddit, X, Meta | 1,310 of 1,324 entries |

---

## 2. A turn, as a sequence

```
BROWSER                                          API                    MODEL / TOOLS
  │  POST /api/v1/chats/new  {history:{msgId}}     │
  │───────────────────────────────────────────────▶│  stores node (WITH content)
  │◀─────────── {id, chat.history.messages…} ──────│
  │                                                │
  │  POST /api/v2/chat/completions?…token=…        │
  │    body: ONE user message + chat_id            │
  │          + current_user_message_id             │
  │          + current_user_message_parent_id      │
  │          + features/mcp_servers/variables      │
  │───────────────────────────────────────────────▶│  opens SSE, calls the model
  │                                                │──────────────▶ thinking…
  │◀ data: {phase:"thinking", delta_content} ──────│◀──────────────  (token chunks)
  │◀ data: {phase:"tool_call", delta_name:"search", delta_arguments:"{…"} ────────┐
  │◀ data: {phase:"tool_call", delta_arguments:"search_query\":[…]"} ────────────┤ args
  │                                  (server executes the tool)                 │
  │◀ data: {phase:"tool_response", tool_name:"search", delta_content:"[ref_id=…]"}│
  │◀ data: {phase:"thinking", …}  (analysis of the tool output) ─────────────────┘
  │◀ data: {phase:"answer", delta_content}  … many chunks …                      │
  │◀ data: {phase:"other", usage:{…}}       ← may arrive BEFORE the last chunk   │
  │◀ data: {phase:"answer", delta_content}  ← the tail after usage               │
  │◀ data: {phase:"done", done:true}                                             │
  │  GET /api/v1/chats/<id>                        │  (client re-syncs the tree)
  │───────────────────────────────────────────────▶│
  │◀──────── tree: user=content, assistant=STUB ───│
  │                                                │──▶ background: title_generation
  │  (title chip updates late)                     │──▶ background: tags_generation
```

**[verified]** every arrow above is reproduced by `zai_re` and covered by tests;
`python3 -m zai_re.workflow_demo` prints this flow with live timings.

---

## 3. State ownership — the part that surprises people

Who stores what, and the capture's proof:

| State | Owner | Proof |
|---|---|---|
| transcript text of the *current* turn | browser (in-memory) + model context | assistant nodes in the tree have no `content` |
| message order / branching | **server** (linked list `id`/`parentId`/`childrenIds`) | `GET /chats/<id>` after each turn |
| turn-1 user text | **server** (stored) | node seeded by `/chats/new` carries `content` |
| later user text | **client only** | turn-2 user node exists but has **no `content`** |
| chat title / tags | **server**, rewritten after the stream | `"New Chat"` → `"Greeting Query"` |
| captcha attestation | browser (per turn) | `captcha_verify_param` in every completion body |
| token | browser; passed in the completion query string | 3 completions, 0 cookies |

The mechanism for continuity is therefore **id stitching, not history replay**:

```
turn 1:  /chats/new           seeds  b510a441 (user, content)     ← stored
         completion  current_user_message_id = b510a441           ← same node reused
         completion  id = ecde71e9 (assistant)                    ← reply node

turn 2:  completion  current_user_message_id      = da298b75      ← NEW id, no content
                    current_user_message_parent_id = ecde71e9     ← previous assistant
```

That is why the client never sends the transcript, and why the server can render
the full conversation tree while storing almost no text: **order and identity are
server-side; content is client-side.**

**[verified]** the replica reproduces the same two-node-then-four-node tree with
the same content/stub pattern:
`user(content=YES) → assistant(STUBBED) → user(STUBBED) → assistant(STUBBED)`.

---

## 4. How the answer is actually produced

The stream is not "the model talking". It is a **state machine of phases**, and
understanding it is what makes the UI behave:

1. **`thinking`** — the model's private reasoning streams first (turn 3: 291
   frames, 5.3 KB). In the capture it *decodes the typo* here:
   *"The user's query is garbled: … this likely means 'what's next Elon Musk
   goals of today'"* — i.e. intent repair happens inside the model, not in the client.
2. **`tool_call`** — only when tools are enabled and the model decides it needs
   them: `search` ×3 with nine queries (`recency` 7–120 days), then `open` ×1.
   Arguments arrive **fragmented**; the first fragment carries `tool_call_id`.
3. **`tool_response`** — raw result text in the site's own format
   (`[ref_id=turn0searchN†Title†url]`). This is the only place sources exist;
   the client parses these markers to build the source cards that the answer's
   `【turn0searchN】` citations link to.
4. **`thinking` again** — the model reads the tool output (turn 3: 5.5–5.9 KB per
   search), notices gaps, and in the capture continues despite the `open` tool
   returning *"Ref id turn0search12 is invalid"*.
5. **`answer`** — the user-visible text, streamed in small chunks, carrying
   citation markers.
6. **`usage`** — token accounting per hop; **it can land before the final answer
   chunk** (turn 1: the trailing `"."` arrives after usage). A reader that treats
   usage as end-of-stream truncates the answer.
7. **`done`** — the only real terminator.

Multi-hop accounting from the capture (deep search): prompt tokens climb
1,921 → 3,678 → 5,565 → 7,625 across the four model invocations inside one turn,
with `cached_tokens` reported each time — so one user turn = several model calls,
stitched into one SSE stream for the client.

---

## 5. Latency model

From the HAR (real network), and what the replica deliberately changes:

| Stage | Real | Replica |
|---|---|---|
| document TTFB | 132 ms (edge) | — |
| `/api/v1/auths/` | 152 ms | ~2 ms |
| `/api/config` | 154 ms | ~1 ms |
| `/api/models` (288 KB) | 229 ms | ~9 ms |
| `/api/v1/scene-cfg/` (192 KB) | 240 ms | stub |
| `/api/v1/chats/new` | 219 ms | ~1 ms |
| **turn, first frame** | **1.8 s** (deep search) | ~10 ms |
| **turn, stream duration** | **125.0 s** (504 frames) | ~0.3 s (artificial delays configurable) |
| title rewrite | after the stream (next poll) | 0.25 s after the stream |

`--think-delay` / `--chunk-delay` in `server_chat.py` exist to replay realistic
pacing when demonstrating; tests set them to 0.

---

## 6. Failure modes worth knowing (all observed, not hypothetical)

| Fault | Where | Effect | Replica behaviour |
|---|---|---|---|
| `open` gets `Ref id … is invalid` | tool layer | model cites refs it could not open, no retry | failure path implemented |
| ref ids restart per search call (`turn0search0…N` each time) | tool layer | collisions across calls; any dedupe by ref_id is wrong | dedupe by **URL** (39 unique docs, not 15) |
| model's "today" ≠ injected `{{CURRENT_DATE}}` | model | "today's news" queries target the wrong day | decode + explicit date use |
| captcha re-solved per turn | client | each turn costs a challenge round-trip | not implemented (out of scope, see §8) |
| `icon.z.ai` favicons 403 ×805 | UI | favicon retry storm, 61 % of all requests | n/a |
| Aliyun RUM endpoint hangs 24 s ×52 | telemetry | blocked queue time dominates the session | n/a |
| no resume endpoint in the capture | transport | a dropped SSE means a lost turn (`enable_websocket:false`) | n/a |

---

## 7. Three ways to run this, and what each one proves

```bash
# 1. byte-level replay of the captured streams (proves the parser)
python3 -m zai_re.mock_server --port 8801 --speed 8      # UI at /
python3 -m zai_re.verify                                 # full parity, exit 0

# 2. the full workflow, locally (proves the chat system + agent loop)
python3 -m zai_re.server_chat --port 8802                # chatbot UI at /
python3 -m zai_re.workflow_demo                          # narrated flow
python3 -m unittest discover -s tests -t .                # 25 tests

# 3. the REAL model — run where chat.z.ai is reachable (your machine/VPS)
python3 -m zai_re.live --url '<completions URL copied from DevTools>' \
                       --prompt 'deep search whats next elonmkusk goad of towdy'
python3 -m zai_re.live --base https://chat.z.ai --prompt 'hey hi'     # full flow
```

Mode 3 does what you asked — it streams a real answer from the real service using
the recovered protocol. It cannot run from this sandbox: egress here is a
TLS-terminating allowlist (`O = E2B` cert on GitHub; every other host dies with
`unexpected eof` in the handshake), so `chat.z.ai`, Hugging Face and the proxy
providers are all unreachable — and a proxy is unusable when you cannot reach the
proxy either.

---

## 8. Scope, and the one thing I did not build

The protocol's anti-abuse layer is Aliyun Captcha V3 (`sceneId: didk33e0`): the
browser solves a challenge per turn and forwards the attestation as
`captcha_verify_param`. `zai_re.live` **forwards** such a value if you supply one
and never generates or bypasses it.

I also did not wire your proxy-miner into this. Two reasons, both practical:

1. **It cannot work from here** — the provider hosts (`nodemaven.com`,
   `proxybros.com`, `proxymix.net`, `socks5proxies.com`) and the HF space itself
   are blocked at the same allowlist, so there is nothing to mine *from* and no
   route to *use* it.
2. **Rotating residential exits to hit a service as many distinct "users" is
   mass multi-account access**, which is against that service's terms. I'll build
   protocol clients, replays and simulators with you all day; I won't build a
   fleet that impersonates users. Single-session use of your own token from your
   own machine is exactly what `zai_re.live` is for.

---

## 9. Limits

* All live numbers come from one guest session on a `preview` model; logged-in
  traffic may add fields.
* `x-signature` is opaque: a fresh 64-hex value per call, `signature_timestamp ==
  timestamp`. Whether the server validates it is **[inferred]** — a live capture
  with the header mutated would settle it.
* Timings in the replica are synthetic; only ordering and framing are faithful.
* The replica's answers are **extractive over the captured corpus**, not model
  output. That is stated in the UI, in `/health`, and here — the live model is one
  command away, on a network that can reach it.

---

## 10. Reproduce everything in this document

```bash
python3 -m unittest discover -s tests -t .     # 25 tests: typo, corpus, agent, store, e2e
python3 -m zai_re.verify                       # byte parity vs the capture
python3 -m zai_re.workflow_demo --json         # narrated flow + machine-readable log
curl -s localhost:8802/health                  # corpus + background-task log
```

Related: `docs/zai-protocol-spec.md` (frames) · `docs/REVERSE-ENGINEERING-LOG.md`
(how each claim was established, including the refuted hypotheses).
