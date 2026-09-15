# chat.z.ai.har — traffic analysis

**File:** `chat.z.ai.har` (55,988,600 bytes on disk) · HAR 1.2 · exported by `WebInspector 537.36` (Chrome 153 DevTools)
**Capture window:** 2026-09-15T16:15:49.499Z → 16:33:06.091Z (17 min 17 s, single page `https://chat.z.ai/`)
**Entries:** 1,324 · network transfer ≈ **24.2 MB** (the file is bigger because binary bodies are stored base64)
**Page timings:** `onContentLoad` 1,128 ms · `onLoad` 7,193 ms · first document byte 132 ms (ESA CDN edge)

This is a session of the **Z.ai chat app (GLM-5.3-Flash / model id `x-preview-l`, "preview mode")** used by a **guest account**, containing a 3-turn conversation, one of which is a *Deep Search* (agent + web-search) run.

---

## 1. What the user actually did

| # | Time (UTC) | Action | Result |
|---|-----------|--------|--------|
| 0 | 16:15:49 | Opens `chat.z.ai` as a new guest | Guest account auto-provisioned |
| 1 | 16:16:08 | `hey hi` | GLM introduces itself (3.4 s stream) |
| 2 | 16:16:22 | `whats ur name ?` | "My name is GLM…" (5.5 s stream) |
| 3 | 16:17:18 | `deep search whats next elonmkusk goad of towdy` | **Deep Search agent run: 125 s, 3 search calls + 1 open call, 42 sources, cited answer** |
| — | 16:19:25 → 16:33:06 | Idle / tab left open | Only retry loops and beacons keep firing |

Conversation id `53e91269-2f31-4496-aba5-7ba44d9df409`, auto-titled **"Greeting Query"** by the server-side title-generation background task.

The full human-readable reconstruction is in [`transcript.md`](./transcript.md) (thinking traces, tool calls, citations, final answers).

---

## 2. Host map

| Host | Reqs | Bytes | Role |
|------|-----:|------:|------|
| `icon.z.ai` | **805** | 0 | News-source favicons for the citation cards — **every single one 403** |
| `sdata.chatglm.cn` | 103 | 36 KB | First-party telemetry pixel (`…/frontend/zai/e.gif?bt=pv/expose/er…`) |
| `zcode.z.ai` | 103 | 60 KB | Mirror of the same telemetry (`/api/v1/zai/blank.gif`) |
| `alb.reddit.com` | 52 | 4 KB | Reddit ad pixel (`rp.gif`, pixel `a2_hm8uup7421lr`) |
| `j2c03hoppk-default-cn.rum.aliyuncs.com` | 52 | 0 | Aliyun ARMS **RUM** SDK beacons — **all aborted after ~24 s** |
| `www.google.com` / `analytics.google.com` / `stats.g.doubleclick.net` | 61 | 1 KB | GA4 (`G-Z8QTHYBHP3`) + Google Ads conversion (`en=first_visit`) |
| `l.clarity.ms` / `scripts.clarity.ms` / `c.clarity.ms` | 37 | 85 KB | Microsoft Clarity session recording (project `187234156`) |
| `z-cdn.chatglm.cn` | 28 | **18.3 MB** | Static app bundle, `prod-fe-1.1.95` |
| `g.alicdn.com`, `o.alicdn.com` | 10 | 1.4 MB | Aliyun Captcha frontend + jQuery |
| `no8xfe*.captcha-open-southeast.aliyuncs.com`, `upload.…` | 15 | 7 KB | **Aliyun Captcha V3** (Init/Verify/UploadLog) — re-run before every completion call |
| `cloudauth-device-dualstack.ap-southeast-1.aliyuncs.com` | 4 | 2 KB | Aliyun **device fingerprinting** (cloudauth, `Action=Log1/2/3`, encrypted `Data=`) |
| `capi-automation.s3.us-east-2.amazonaws.com` | 1 | 64 KB | Meta CAPI `clientParamBuilder` (server-side conversion helper) |
| `chat.z.ai` | 14 | 177 KB | The only first-party API host (see §4) |
| `www.googletagmanager.com`, `connect.facebook.net`, `bat.bing.com`, `static.ads-twitter.com`, `t.co`, `analytics.twitter.com`, `8b-…on.aws` | 24 | 3.2 MB | GTM container, Meta Pixel (`1921933375117245`), Bing UET, Twitter Ads, "smart setup" page-view analyzer |

Three tracking pipelines run in parallel for the same events: the first-party `e.gif`/`blank.gif` pair, the Aliyun RUM SDK, and the usual ad-tech stack (GA4/Google Ads, Clarity, Bing UET, Reddit, X/Twitter, Meta).

---

## 3. Auth, identity and anti-abuse surface

* **Guest session, no cookies at all.** No `Cookie` header on any of the 1,324 requests. Identity travels in a **JWT in the URL query string**:
  `…/api/v2/chat/completions?…&token=eyJhbGciOiJFUzI1NiIs…&user_id=4d00e18b-7368-4b01-95f6-a8bf70f59cda&…`
  JWT header is `ES256`; payload is `{"id":"4d00e18b-…","email":"guest-1789488950420@guest.com"}`. Returning from `/api/v1/auths/` as `token_type: Bearer`, guest role with `chat.temporary_enforced: true`.
* **Full browser fingerprint in the query string** next to the token: UA, language(s), timezone (`Asia/Calcutta`), screen/viewport/pixel-ratio, `cookie_enabled`, platform, `local_time`, `signature_timestamp`, current URL/title, etc.
* **Custom anti-bot headers on completion calls only:** `x-signature` (32-byte hex, distinct per call), `x-device-id: uid_mj2ec9ucjvyza39n`, `x-fe-version: prod-fe-1.1.95`, `x-region: overseas`.
* **Aliyun Captcha V3 is re-solved before each turn** (`didk33e0` scene) and the result is forwarded in the body as `captcha_verify_param` (base64 blob containing `certifyId`, `sceneId`, `isSign`, `securityToken`).
* **Device fingerprint POSTs** to `cloudauth-…aliyuncs.com` with HMAC-SHA1-signed, AES-encrypted `Data=` payloads accompany the captcha flow.
* `x-device-id` (`uid_mj2ec9ucjvyza39n`) and the user id also appear in the Aliyun RUM beacon payloads, so the same identifier ties RUM ↔ telemetry pixels ↔ API calls ↔ captcha.

**Security notes worth flagging to whoever owns this app:**
1. The bearer token is in the **query string** of a streaming POST — it lands in CDN/proxy/access logs, browser history-free but referrer-adjacent, and any log pipeline sees it. Prefer `Authorization` header (the API already supports `Bearer`).
2. `GET /api/config` publicly advertises MCP servers, including one named **`exfil-server` — title "Internal SSRF", description "SSRF test server"**, alongside `deep-web-search` and `ppt-maker`. Whatever its purpose, publishing that name/description in an unauthenticated config response is a red flag (the deep-search turn actually invoked an MCP server named `advanced-search`, which is *not* in that list).

---

## 4. The chat API contract (reverse-engineering notes)

Four first-party endpoints carry the whole app:

```
GET  /api/v1/auths/                  -> guest identity + token + permissions
GET  /api/config                     -> feature flags, default model, MCP server list
GET  /api/models                     -> 15-model catalog with capabilities/params (287 KB)
GET  /api/v1/scene-cfg/?model=<id>   -> landing-page scene config / prompt library (192 KB)
POST /api/v1/chats/new               -> creates the chat, returns the message tree
POST /api/v2/chat/completions        -> SSE stream of the answer
GET  /api/v1/chats/<chat_id>         -> re-fetch server-side message tree
POST /api/v1/users/user/settings/update  {"ui":{"timezone":"Asia/Calcutta"}}
```

### Request shape (`/api/v2/chat/completions`)

The client sends **only the new user message**, not the history — the server keeps the conversation state and stitches it from `chat_id` + `current_user_message_parent_id`:

```json
{
  "stream": true, "model": "x-preview-l",
  "messages": [{"role": "user", "content": "hey hi"}],
  "signature_prompt": "hey hi",
  "features": {"image_generation": false, "web_search": false, "auto_web_search": true,
               "preview_mode": true, "enable_thinking": true, "reasoning_effort": "max", ...},
  "variables": {"{{CURRENT_DATE}}": "2026-09-15", "{{CURRENT_TIMEZONE}}": "Asia/Calcutta",
                "{{USER_NAME}}": "Guest-1789488950420", "{{USER_LOCATION}}": "Unknown", ...},
  "chat_id": "53e91269-…", "id": "<assistant message id>",
  "current_user_message_id": "<user msg id>", "current_user_message_parent_id": "<prev assistant msg id>",
  "background_tasks": {"title_generation": true, "tags_generation": true},
  "mcp_servers": ["advanced-search"],
  "captcha_verify_param": "<base64>"
}
```

### Response: `text/event-stream`, one JSON per frame

```json
data: {"type":"chat:completion","data":{"delta_content":"Hello! I'm GL","phase":"answer"}}
data: {"type":"chat:completion","data":{"phase":"tool_call","delta_name":"search",
       "delta_arguments":"{\"search_query\":[…", "metadata":{"tool_call_id":"call_…","type":"function"}}}
data: {"type":"chat:completion","data":{"phase":"tool_response","tool_name":"search",
       "status":"completed","delta_content":"[ref_id=turn0search0†Title†https://…]", "metadata":{…}}}
data: {"type":"chat:completion","data":{"phase":"other","usage":{"prompt_tokens":14,
       "completion_tokens":184,"total_tokens":198,"prompt_tokens_details":{…}}}}
data: {"type":"chat:completion","data":{"phase":"done","done":true}}
```

Phases observed: `thinking` (reasoning stream), `tool_call`, `tool_response`, `answer`, `other` (usage), `done`. Only one event type is used — `chat:completion`. Tool arguments arrive **chunked**, so callers must concatenate `delta_arguments` until the JSON closes (the first chunk carries `metadata.tool_call_id`, later chunks only `metadata.type`).

### Conversation state model

A **linked list** per chat: each message has `id`, `parentId`, `childrenIds`, `role`, `timestamp`, and content is stored **server-side only for user messages** — assistant messages come back as stubs (`role: assistant`, no `content`), which is why the client re-fetches `/api/v1/chats/<id>` after a turn. `message_version: 1`, `meta.workspace_id == chat id`.

---

## 5. The Deep Search run (turn 3) — 125 seconds

Trigger: the user's garbled `deep search whats next elonmkusk goad of towdy` → the client set `mcp_servers: ["advanced-search"]` and `auto_web_search: true` (previously `false`), model kept `x-preview-l` with `reasoning_effort: "max"` and thinking on.

Model behaviour, in order (504 SSE frames):

1. **Thinking:** decodes the typo ("what's next Elon Musk goals of today"), notes a conflicting date hint ("the initial search guidance mentioned 2025‑07‑14… I should use the actual current date: September 16, 2026").
2. **3 × `search` tool calls** (9 queries total), each with a `recency` window in days:
   * `Elon Musk next plans 2026` (14), `…announcement today` (7), `…latest news September 2026` (14)
   * `…xAI Grok announcement` (7), `…Starship Mars launch 2026` (30), `…news September 14 15 16 2026` (7)
   * `…first trillionaire SpaceX IPO June 2026` (120), `SpaceXAI Grok 5 launch date AGI` (60), `Tesla robotaxi Optimus 2026 update` (30)
3. **Tool responses** come back as one text blob of `[ref_id=turn0searchN†Title†https://domain]` blocks + page extracts (≈5.5–5.9 KB each, **42 distinct sources**: Reuters, AP, NYT, WSJ, Wired, CNBC, The Guardian, TIME, X.ai, SpaceX, Wikipedia, Polymarket, LinkedIn, YouTube, Facebook, blog spam, …).
4. **1 × `open` tool call** on `turn0search12` and `turn0search10` (lineno 1) — both returned **`Ref id turn0search12 is invalid / Ref id turn0search10 is invalid`** in 64 bytes. The agent took no corrective action and continued.
5. **Final answer** — a structured Markdown brief with emoji headings and inline citations in the form `【turn0search3】`, mixing figures from different sources (e.g. "$1.7T" vs "$2.1T" valuation, SpaceX IPO Jun 12 vs Jun 14).

Token accounting after each hop (cumulative prompt): 1,921 → 3,678 → 5,565 → 7,625 prompt tokens, ending at 1,578 completion tokens, with **cached prompt tokens** reported throughout (`cached_tokens` 1,536 / 1,920 / 3,648 / 5,504) — prefix caching is clearly active.

Timing: 1.8 s to first byte, then **123 s of continuous streaming** (`receive`), ~92 KB decoded. This single request accounts for **89 % of the session's total receive time** (123.2 s of 138.6 s).

*Caveat:* the search corpus/answers describe a mid-2026 world (SpaceX IPO, trillionaire milestone, Grok 5, G20 quotes). They are reproduced here as what the tool returned; nothing in the HAR indicates the model fabricated them — but two internal inconsistencies are visible (the "September 16" reasoning vs the injected `2026-09-15`, and the "invalid ref id" for citations the answer nonetheless leans on).

---

## 6. Findings / issues, in priority order

| # | Severity | Finding | Evidence |
|---|----------|---------|----------|
| 1 | **High (wasted work)** | `icon.z.ai` favicon service rejects the app: **805 requests, 100 % HTTP 403**, `x-tengine-error: denied by Referer ACL`. The page itself declares `<meta name="referrer" content="no-referrer">`, so every cross-origin image request arrives with an empty `Referer` and is denied — the app can never receive this header. Only 21 distinct URLs exist, but the same URL is fetched up to **94×** in 98 s (`nypost.com` 94, `space.com` 92, `reuters.com` 92, `cnbc.com` 90) → an image-`onerror` → re-render/retry loop. 60.8 % of all HAR entries and 315 s of cumulative "blocked" queue time. Fix: allow empty-referrer requests server-side, or proxy/cache favicons first-party and memoise failed loads client-side. | entries 377–1204 |
| 2 | **High (page hangs)** | Aliyun RUM endpoint is unreachable from this network: **52 `POST`s to `j2c03hoppk-default-cn.rum.aliyuncs.com`, all status 0**, each timing out after **~24.2 s** before being retried — 977 s of cumulative blocked time, the single biggest time sink in the capture. The SDK's own log even reports `{"level":"error","content":"Request failed after 1 attempts","stack":"TypeError: Failed to fetch"}` and the app emits `global_network_error / ctvl=Failed to fetch` beacons every few seconds until 16:33. Fix: hard timeout + backoff + drop the queue after N failures instead of a 24 s blocking retry. | entries 90, 163, 332, 370, 463, 750, 1199–1317 |
| 3 | **Medium (payload)** | 18.3 MB of first-party JS/CSS for a chat page, and **10.6 MB of it is one file** — `XlsxUniverViewer-*.js` — loaded although no spreadsheet was ever opened (the whole PDF/DOCX/PPTX/XLSX viewer family is fetched up-front: 1.25 MB Pptx, 0.45 MB pdf, 0.34 MB Docx). Also `gtag/js` is downloaded **three times** (three GA properties: `G-Z8QTHYBHP3`, `G-9HHB4R5K1T`, `G-SF8X67RPF9`) and `connect.facebook.net` pulls a 0.49 MB config blob. | entries 108, 115, 96, 112, 1/11/12, 35 |
| 4 | **Medium (hygiene)** | Bearer token + full device fingerprint in the completion URL query string; `x-signature` exists but is not used to authenticate the call. Logs everywhere will contain the credential. | entry 237/285/355 URL |
| 5 | **Medium (info leak)** | `GET /api/config` (unauthenticated) publishes an MCP server entry `{"name":"exfil-server","title":"Internal SSRF","description":"SSRF test server"}`. | entry 21 body |
| 6 | **Low** | Dead asset: `GET https://z-cdn.chatglm.cn/static/logo.png` → **404**. | entry 86 |
| 7 | **Low** | Duplicate telemetry: every event is sent twice (`e.gif` *and* `blank.gif`) with the same `eid`, so the first-party event count is inflated 2× (103 + 103 of the 269 `image/gif` responses in the capture). | all e.gif/blank.gif pairs |
| 8 | **Low** | Agent robustness: the `open` tool returned `Ref id … is invalid` for 2 of the refs the model asked for, and the model proceeded to cite them anyway. No retry/repair path. | entry 355, event 185 |
| 9 | **Low** | Model date confusion: prompt variable says `2026-09-15 21:47 IST`, the model reasons "Today is September 16, 2026" (its own local time), so "today's news" queries are built from the wrong day. | entry 355 thinking |
| 10 | **Info** | Zero cookies set — consistent with a `temporary_enforced` guest chat; all state is server-side and keyed by the URL token/`chat_id`. | all entries |

---

## 7. Model catalogue captured (from `/api/models`)

| id | name | notes |
|----|------|-------|
| `x-preview-l` | **GLM-5.3-Flash** | Used here. `preview_mode` in request, max_tokens 128 000, temp 1, top_p 0.95, vision, thinking, reasoning_effort, web_search |
| `glm-5.3` / `glm-5.2` | GLM-5.3 / GLM-5.2 | thinking + MCP + web_search |
| `GLM-5-Turbo`, `GLM-5v-Turbo` | — | 5v is the multimodal/citation variant (vlm_* flags on) |
| `glm-4.7`, `glm-4.6v`, `glm-4-flash`, `glm-4-air-250414` | GLM-4.x family | 4.6v = vision-only, `任务专用` = task-specialised flash |
| `0727-106B-API` / `0727-360B-API` | GLM-4.5-Air / GLM-4.5 | temp 0.6 |
| `GLM-4.1V-Thinking-FlashX` | GLM-4.1V-9B-Thinking | |
| `deep-research`, `zero`, `0808-360B-DR` | Z1-Rumination / Z1-32B | citation & deep-research specialists |

Feature flags from `/api/config`: `enable_captcha: true`, `enable_mcp: true`, `enable_artifacts_mode: true`, `enable_upload_image: false`, `enable_websocket: false`, `default_models: "glm-5.3"`, `completion_version: "2"`, OAuth providers google/github/maas. Frontend build: `prod-fe-1.1.95`.

---

## 8. Reproducing this analysis

```bash
python3 tools/har_explore.py summary      # hosts, statuses, sizes, mime types
python3 tools/har_explore.py apis         # first-party endpoints + request bodies
python3 tools/har_explore.py transcript   # full conversation, thinking, tool calls, sources
python3 tools/har_explore.py issues       # failures, duplicate fetches, slowest, error strings
python3 tools/har_explore.py timing       # phase totals, per-host blocked time, request timeline
```

All commands handle Chrome's base64-encoded bodies and the chunked SSE format. `transcript` regenerates [`transcript.md`](./transcript.md).

---

## 9. Caveats

* `_transferSize`/`content.size` are unreliable for the 52 aborted RUM posts and the 403 favicons (reported as 0 B) — real cost is round-trips, not bytes.
* HAR 1.2 from Chrome omits WebSocket frames; the app had websockets disabled anyway (`enable_websocket: false`), and all completions used SSE.
* Reconstructed prompt/system text is only visible through the injected `variables` block and model behaviour; the system prompt itself is never in the capture.
* The session is a **guest** session on a **preview** model — behaviour may differ from logged-in/production traffic.
