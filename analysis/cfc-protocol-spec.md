# How the Cloudflare AI Playground client was reverse engineered

Reconstructed from the `Allase` clone (2026-09-15): `provider.py`, `mock.py`, `pipeline.py`, `relay.py`,
`README.md` on every branch, `previoslog.md`, plus the older `Adarshukumar/cloud` snapshot of the same code.

The short version: **a real browser was used once to watch the traffic (v1, "LS"), then the browser was
thrown away and the exact same frames were replayed from pure Python (v2+) — with Chrome's TLS
fingerprint borrowed from `curl_cffi` to survive the edge.**

---

## Stage 0 — the original approach was a browser

`provider.py` header says it outright:

```
cloudflare_provider.py  —  LS-v2 (pure Python, no browser)
Reverse-engineered Cloudflare AI Playground client.
v2 changes vs the original LS project:
  • PRIMARY transport: curl_cffi.WebSocket with impersonate="chrome"
    → real Chrome TLS/JA3/JA4 fingerprint, HTTP/2, proper headers. NO browser, NO Xvfb.
  • FALLBACK transport: websocket-client (plain Python WS).
  • Same protocol layer: handshake, RPC getModels, cf_agent_state,
    cf_agent_use_chat_request, streaming reasoning-delta / text-delta parsing.
```

So v1 ("LS") drove a real Chrome (headless under **Xvfb**) at `playground.ai.cloudflare.com` and let it be
the thing that opened the WebSocket. Everything below was *observed* there — the phrase "same protocol
layer" means v2 is a faithful replay of frames that were already captured, not a re-discovery.
(Browser automation used in v1 never appears in this repo: no selenium/playwright/puppeteer anywhere.)

## Stage 1 — reading the wire

The playground is a **Cloudflare Agents** app (`agents` npm package) on top of the **Vercel AI SDK**.
That shows up in the shapes it exchanges.

**Endpoint** (`provider.py` `_make_sid/_make_pk/_make_ws_url`):

```
wss://playground.ai.cloudflare.com/agents/playground/Cloudflare-AI-Playground-<21 rand chars>?_pk=<uuid4>
```

**Server → client, on connect (the handshake).** `provider.py:595` waits for exactly these three:

| frame | meaning |
|---|---|
| `cf_agent_identity` | server identifies itself |
| `cf_agent_state` | current playground settings (model, temperature, stream, system, …) |
| `cf_agent_mcp_servers` | MCP tool servers the agent has |

**Client → server frames** (the whole vocabulary, counted across all branches):

| frame | shape | purpose |
|---|---|---|
| `cf_agent_stream_resume_request` | `{"type":…}` | "any stream to resume?" → answered with `cf_agent_stream_resume_none` or the tail of a live stream |
| `rpc` | `{"args":[],"id":<uuid>,"method":"getModels","type":"rpc"}` | pulls the model catalogue; reply carries the **same `id`** + `done`/`success`/`result` |
| `cf_agent_state` | `{"type":…,"state":{model,temperature,stream,system,useExternalProvider:false,externalProvider:"openai",authMethod:"provider-key",maxTokens}}` | pushes settings. These exact keys are the playground's own settings panel — including the "bring your own provider key" toggle — i.e. copied off the UI state messages |
| `cf_agent_use_chat_request` | `{"id":…,"init":{"method":"POST","body":"<json string>"},"type":…}` | the actual chat turn: an HTTP-shaped request tunnelled inside a WS frame |
| `cf_agent_chat_request_cancel` | `{"type":…}` | abort a generation (used for race losers) |
| `cf_agent_chat_clear` | `{"type":…}` | reset the conversation |

**The body inside `cf_agent_use_chat_request` is an AI-SDK `useChat` payload** — the giveaway:

```json
{"messages": [
   {"role":"user","id":"<16 rand>","parts":[{"type":"text","text":"hello"}]},
   {"id":"assistant_<epoch_ms>_<9 rand>","role":"assistant",
    "parts":[{"type":"step-start"},
             {"type":"reasoning","text":"…","state":"done"},
             {"type":"text","text":"…","state":"done"}]}],
 "trigger":"submit-message"}
```

`parts[]`, `step-start`, `state:"done"`, `trigger:"submit-message"`, and `assistant_<ms>_<rand>` ids are
AI SDK UIMessage fields. An equivalent `to_cf()` / `_Build` converter now lives in `provider.py`.

**Server → client, streaming** (`provider.py:864+`):

```json
{"type":"cf_agent_use_chat_response","id":"<request id>","body":"<json STRING>","done":false}
```

The `body` is itself JSON, and its inner `type` drives the parser:

`start` → `reasoning-start` / `reasoning-delta{delta}` / `reasoning-end` → `text-delta{delta}` → `text-end` → `done:true`, plus `error{message}`.
The client maps that onto `<think>…</think>` + answer text, keeping reasoning separate from output.

## Stage 2 — replaying it without a browser

Two walls, both discovered by hitting them:

1. **TLS fingerprinting.** Plain `websocket-client` gets dropped. `provider.py:293` explains the fix:
   > *"Cloudflare fingerprints the TLS handshake (JA3/JA4). curl_cffi swaps OpenSSL for BoringSSL and sends
   > the exact cipher order / HTTP2 settings of a real Chrome build."*
   `previoslog.md` keeps the raw failure: `curl: (35) BoringSSL SSL_connect: Connection closed abruptly …
   in connection to playground.ai.cloudflare.com:443`. So: `curl_cffi.WebSocket(impersonate="chrome")`
   as transport #1, plain `websocket-client` as transport #2, and a clear `ConnectionError` telling the
   operator to run from a usable IP.
2. **IP gating.** `provider.py:586` prints the failure it saw in practice:
   > *"playground.ai.cloudflare.com is behind Cloudflare Access (error 1050 on some IPs). Run from a
   > residential/unblocked IP."*

   Error **1050** = Cloudflare Access policy denial. `main.py` still probes for it
   (`if "1050" in text or "access" in text or status in (403,1020)`). This single fact is the origin of the
   entire later roadmap: *whose IP does Cloudflare see?*

## Stage 3 — proving the spec without the network

`mock.py` is the same protocol re-implemented **as a server** (FastAPI, `@app.websocket("/agents/playground/{session_id}")`),
emitting `cf_agent_identity` / `cf_agent_state` / `cf_agent_mcp_servers` on connect,
`cf_agent_stream_resume_none` for resume, an `rpc` echo for `getModels`, and
`cf_agent_use_chat_response` frames built exactly like the real ones (`_handle_chat` → `frame()`).

This is the validation move that closes an RE loop: if the re-implemented client drives the
re-implemented server, the recovered protocol is almost certainly right.
It also carries a test hook — a prompt containing `[delay:5]` makes the mock stall before the first frame,
so the ×2 → ×4 **race escalation** can be triggered deterministically. `tests/fake_relay.py` does the
same job for the v3.2 relay bridge.

Downstream, the `getModels` reply became `cache/cloudflare_models.json` (a 79 KB dump of CF's catalogue,
TTL 6 h), and that file is what `_resolve()` / `_ctx_window()` read to map short names and context windows.

## Stage 4 — facts learned later that redesigned the architecture

* **A server cannot make Cloudflare see a user's IP.** Verified empirically (`previoslog.md` ~line 607):
  binding `curl_cffi` to a bogus source address fails (`curl: (45) bind failed`) — TCP handshakes can't be
  spoofed. This is why the project split into:
  * **Pool** (`/`) — server holds the sockets, CF sees the server's IP (needs a clean egress).
  * **Direct** (`/direct`) — the *browser* opens the CF WebSocket, so CF sees the visitor's IP and the server
    is control-plane only.
  * **Relay bridge** (`/relay`, v3.2) — browsers keep one persistent WS to the server and act as lane
    providers on command: the "user IP pool" the whole design was chasing.
* **Origin is checked loosely.** The README notes the browser sends `Origin: <your server>` when opening the
  CF socket and *"the playground currently accepts it; if Cloudflare ever enforces its own origin, use an
  extension/userscript to set the Origin, or fall back to Pool mode"* — i.e. this is the fragile hinge of
  direct/relay mode.
* **Race behaviour** (`pipeline.py` / `relay.py`): the same `cf_agent_use_chat_request` id is fired on 2
  sockets at once; the first frame wins; losers get `cf_agent_chat_request_cancel` and return to the pool;
  silence for 10 s escalates to ×4.

---

## Recovered protocol, in one screen

```
CONNECT  wss://playground.ai.cloudflare.com/agents/playground/{Cloudflare-AI-Playground-<21>}?_pk={uuid}

  S→C  cf_agent_identity        {identity:{…}}
  S→C  cf_agent_state           {state:{…}}
  S→C  cf_agent_mcp_servers     {servers:[…]}
  C→S  {"type":"cf_agent_stream_resume_request"}     →   S→C cf_agent_stream_resume_none
  C→S  {"type":"rpc","id":U,"method":"getModels","args":[]}
                                                      →   S→C {"type":"rpc","id":U,"done":true,"success":true,"result":[…]}
  C→S  {"type":"cf_agent_state","state":{model,temperature,stream,system,maxTokens,
                                         useExternalProvider:false,externalProvider:"openai",authMethod:"provider-key"}}
  C→S  {"type":"cf_agent_use_chat_request","id":R,
        "init":{"method":"POST","body":"{\"messages\":[…parts…],\"trigger\":\"submit-message\"}"}}
  S→C* {"type":"cf_agent_use_chat_response","id":R,"body":"{\"type\":\"reasoning-delta\",\"delta\":\"…\"}","done":false}
                                          … start / reasoning-start / reasoning-delta / reasoning-end /
                                            text-delta / text-end / error … → done:true
  C→S  {"type":"cf_agent_chat_request_cancel"}    |   {"type":"cf_agent_chat_clear"}
```

## Timeline of what came from where

| Date | Event |
|---|---|
| ≤ 2026-08-23 | "LS" v1 — browser (Xvfb) client, protocol observed |
| 2026-08-23 | `c049cfb` first commit of CFC v3 (11 files, 4827 lines) — browser already gone |
| 2026-08-31 | same code pushed to the sibling repo `Adarshukumar/cloud` (older snapshot of the same project) |
| 2026-09-10 | `main` "Add files via upload" + `graphify-out/` code-graph run on `master` |
| 2026-09-10 | v4 branch — per-visitor identity, lanes, egress rotation |
| 2026-09-11 | Docker + proxy mining + middle-man XFF gate branch |
| 2026-09-13 | v3.2 `relay.py` — persistent browser WS pool, races across user IPs (**newest code in the repo**) |

## Caveats worth keeping in mind

* This works because the playground endpoint currently accepts a non-browser client *if* the TLS fingerprint
  and IP look plausible; it is a hosted, rate-limited service, and the repo's own README flags the
  origin/ToS fragility. Nothing here changes if CF starts enforcing its own Origin or auth.
* `_pk` is treated as an opaque per-session value; its server-side meaning was never established.
* `cf_agent_*` names come from Cloudflare's `agents` package, not from guessing — that's why the vocabulary
  is consistent across client, mock, and relay.

*(Same method as the `chat.z.ai.har` work in this repo: capture → identify frame vocabulary → reimplement →
validate against a mock.)*
