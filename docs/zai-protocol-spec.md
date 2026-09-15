# chat.z.ai — recovered protocol specification

Source of truth: `chat.z.ai.har` (1,324 entries, 2026-09-15 16:15:49 → 16:33:06 UTC, Chrome 153).
Everything below is *observed*, not guessed; each claim is backed by a HAR entry or a byte-parity test
(`python3 -m zai_re.verify` → full parity on all three captured turns).

Working implementation: `zai_re/` (stdlib only) · replay server: `python3 -m zai_re.mock_server`.

---

## 1. Transport and surface

| | |
|---|---|
| Origin | `https://chat.z.ai` |
| Edge | `Server: ESA` (Alibaba/EdgeOne), `via: ens-cache*.sg25`, `x-site-cache-status: DYNAMIC` |
| Cookies | **none** — zero `Cookie` request headers and zero `Set-Cookie` responses in 1,324 entries |
| Auth | bearer JWT, sent **in the query string of the completions call only** |
| Streaming | `Content-Type: text/event-stream; charset=utf-8`, `Transfer-Encoding: chunked`, br-compressed |
| CORS | `access-control-allow-origin: https://chat.z.ai`, `access-control-expose-headers: X-Chat-Id, X-Trace-ID` |

## 2. Bootstrap sequence (what the page does on load)

```
GET  /api/v1/auths/                       → guest identity + JWT (auto-provisioned on first visit)
GET  /api/config                          → feature flags, default model, MCP server list
GET  /api/models                          → 15-model catalogue (~288 KB)
GET  /api/v1/scene-cfg/                   → landing/scene config (~192 KB)
GET  /api/v1/scene-cfg/?model=<id>        → same, pinned to a model
GET  /api/v1/users/user/settings          → {"ui":{"timezone":"Asia/Calcutta"}}
POST /api/v1/users/user/settings/update   → same shape, persists the timezone
```

**None of these carry credentials.** No `Authorization` header, no token parameter — verified across all
non-completion first-party calls. `x-region: overseas` is attached to most of them.

## 3. Chat lifecycle

### 3.1 Create

```http
POST /api/v1/chats/new
content-type: application/json
x-region: overseas
```
```json
{"chat":{
  "id":"",  "title":"New Chat",  "models":["x-preview-l"],  "params":{},
  "history":{"messages":{"<user-msg-uuid>":{
      "id":"<user-msg-uuid>","parentId":null,"childrenIds":[],
      "role":"user","content":"hey hi","timestamp":1789488967,
      "models":["x-preview-l"]}},
    "currentId":"<user-msg-uuid>"},
  "tags":[], "flags":[],
  "features":[{"server":"tool_selector_h","status":"hidden","type":"tool_selector"}],
  "mcp_servers":[], "enable_thinking":true, "reasoning_effort":"max",
  "auto_web_search":false, "message_version":1, "extra":{},
  "timestamp":1789488967892, "type":"default"}}
```
Response adds server-side fields: `id`, `user_id`, `updated_at`, `created_at`, `share_id`, `archived`,
`pinned`, `meta.workspace_id`, `folder_id`, `message_version`, `type`, `im_context`.
The title is later rewritten by a background task (`"New Chat"` → `"Greeting Query"`).

### 3.2 Complete (the agent turn)

```http
POST /api/v2/chat/completions
  ?timestamp=&requestId=<uuid>&user_id=<id>&version=0.0.1&platform=web&token=<JWT>
  &user_agent=&language=&languages=&timezone=&cookie_enabled=&screen_width=&screen_height=
  &screen_resolution=&viewport_height=&viewport_width=&viewport_size=&color_depth=&pixel_ratio=
  &current_url=&pathname=&host=&hostname=&protocol=&title=&timezone_offset=&local_time=&utc_time=
  &is_mobile=&is_touch=&max_touch_points=&browser_name=&os_name=&signature_timestamp=
content-type: application/json
x-region: overseas
x-device-id: uid_<16 chars>          (completion calls only)
x-fe-version: prod-fe-1.1.95
x-signature: <64 hex>                (fresh per call; not validated against the body in this capture)
origin: https://chat.z.ai
```
34 query parameters: 1 credential + 1 identity (`user_id`) + **32 browser-fingerprint fields**.
The body:

```json
{"stream":true, "model":"x-preview-l",
 "messages":[{"role":"user","content":"hey hi"}],          ← ONLY the new message
 "signature_prompt":"hey hi",                                ← equals the user text
 "params":{}, "extra":{},
 "features":{"image_generation":false,"web_search":false,"auto_web_search":false,
             "preview_mode":true,"flags":[],"vlm_tools_enable":false,
             "vlm_web_search_enable":false,"vlm_website_mode":false,
             "enable_thinking":true,"reasoning_effort":"max"},
 "variables":{"{{USER_NAME}}":"Guest-1789488950420","{{USER_LOCATION}}":"Unknown",
              "{{CURRENT_DATETIME}}":"2026-09-15 21:46:11","{{CURRENT_DATE}}":"2026-09-15",
              "{{CURRENT_TIME}}":"21:46:11","{{CURRENT_WEEKDAY}}":"Tuesday",
              "{{CURRENT_TIMEZONE}}":"Asia/Calcutta","{{USER_LANGUAGE}}":"en-US"},
 "chat_id":"53e91269-2f31-4496-aba5-7ba44d9df409",
 "id":"<assistant-msg-uuid>",
 "current_user_message_id":"<user-msg-uuid>",
 "current_user_message_parent_id":"<previous assistant uuid or null>",
 "background_tasks":{"title_generation":true,"tags_generation":true},
 "mcp_servers":["advanced-search"],          ← only present for deep search
 "captcha_verify_param":"<base64 Aliyun Captcha V3 attestation>"}
```

Key consequences:

* **Server-side history.** The client never resends the conversation; continuity is
  `chat_id` + `current_user_message_parent_id`. That parent id is the previous *assistant* message id.
* **The tree is a linked list.** Each message has `id`/`parentId`/`childrenIds`. Assistant nodes come back
  from `GET /api/v1/chats/<id>` as **stubs** (`role:"assistant"`, no `content`) — the text lives only in the
  stream, which is why the browser re-fetches the tree after a turn.
* `{{CURRENT_*}}` variables are placeholders resolved by the server; the client ships them as literal keys.
* Deep search differs by exactly two fields: `mcp_servers: ["advanced-search"]` and
  `features.auto_web_search: true`.

### 3.3 Re-sync

```http
GET /api/v1/chats/<chat_id>    →  the whole chat object incl. message tree (assistant text still stubbed)
```

## 4. The stream format

Every frame is `data: <json>\n\n`. **One event type**: `chat:completion`.
`data.phase` selects the interpretation:

| phase | payload | meaning |
|---|---|---|
| `thinking` | `delta_content` | private reasoning, streamed token-chunk |
| `answer` | `delta_content` | the user-visible answer, streamed token-chunk |
| `tool_call` | `delta_name`, `delta_arguments`, `metadata` | a tool invocation; **arguments arrive as fragments** |
| `tool_response` | `tool_name`, `status`, `delta_content` | the tool's result text |
| `other` | `usage` | token accounting for that hop (can appear mid-answer) |
| `done` | `done: true` | terminal frame |

```json
data: {"type":"chat:completion","data":{"delta_content":"Hello! I'm GL","phase":"answer"}}
data: {"type":"chat:completion","data":{"phase":"tool_call","delta_name":"search",
       "delta_arguments":"{\"", "metadata":{"tool_call_id":"call_efb155142f12474ebf94d450","type":"function"}}}
data: {"type":"chat:completion","data":{"phase":"tool_call","delta_arguments":"search_query\":[{\"q\": \"Elon",
       "metadata":{"type":"function"}}}
data: {"type":"chat:completion","data":{"phase":"tool_response","tool_name":"search","status":"completed",
       "delta_content":"[ref_id=turn0search0†Title†https://domain]\nDate: …","metadata":{"browser":{…},"tool_call_id":"call_…"}}}
data: {"type":"chat:completion","data":{"phase":"other","usage":{"prompt_tokens":1921,"completion_tokens":206,
       "total_tokens":2127,"prompt_tokens_details":{"cached_tokens":1536}}}}
data: {"type":"chat:completion","data":{"phase":"done","done":true}}
```

Three implementation traps, each observed in the capture:

1. **`usage` can arrive before the last `answer` delta.** In turn 1 the final `"."` arrives *after* the usage
   frame. Only `phase == "done"` terminates the stream. (A naive client truncates the answer.)
2. **`tool_call` metadata changes mid-call.** The first fragment carries
   `{"tool_call_id": …, "type": "function"}`; later fragments carry only `{"type": "function"}`. The call id
   must be captured from the first fragment; arguments must be concatenated until the JSON closes.
3. **Frames can be arbitrarily large** — `tool_response` frames up to ~14 KB — so a line/`\n\n`-oriented
   parser with an unbounded buffer is required (no fixed chunk assumptions).

Stream statistics for the capture:

| turn | prompt | frames | phases | wall time |
|---|---|---|---|---|
| 1 | `hey hi` | 39 | thinking 29 · answer 8 · usage 1 · done 1 | 3.4 s |
| 2 | `whats ur name ?` | 21 | thinking 9 · answer 10 · usage 1 · done 1 | 5.5 s |
| 3 | `deep search whats next elonmkusk goad of towdy` | 504 | thinking 291 · tool_call 31 · tool_response 4 · answer 173 · usage 4 · done 1 | 125.0 s |

## 5. The agent (deep search) behaviour

Tool surface seen: `search` (`{"search_query":[{"q":…,"recency":<days>}]}`) and `open`
(`{"open":[{"ref_id":"turnNsearchM","lineno":1}]}`).

```
thinking (decode intent) → search ×3 (9 queries, recency 7–120 days)
   → tool_response ×3 (≈5.5–5.9 KB each of "[ref_id=turn0searchN†Title†url]\nDate: …\n<snippet>")
   → open ×1 → tool_response: "Ref id turn0search12 is invalid / turn0search10 is invalid" (64 B)
   → thinking (analyze) → answer with 【turn0searchN】 citations → done
```

Observations worth keeping:

* The model **does not retry** after the invalid `open`; it cites the refs anyway.
* Search results are injected as plain text with `[ref_id=…†title†url]` headers; the answer cites them as
  `【turn0searchN】`, which is how the UI can render source cards.
* Its own reasoning says "Today is September 16, 2026" while the injected `{{CURRENT_DATE}}` is `2026-09-15` —
  the model used local wall-clock, not the variable. "Today's news" queries are therefore built for the wrong
  day when the client's timezone crosses midnight relative to UTC.
* `cached_tokens` appears in every usage frame (1536 → 1920 → 3648 → 5504), i.e. prefix caching is real.

## 6. Models

`GET /api/models` returns `data[]` with `id`, `name`, `info.params`, `info.meta.capabilities`.
15 entries: `x-preview-l` (GLM-5.3-Flash, used here), `glm-5.3`, `glm-5.2`, `GLM-5-Turbo`, `GLM-5v-Turbo`,
`glm-4.7`, `glm-4.6v`, `glm-4-flash`, `glm-4-air-250414`, `0727-106B-API`, `0727-360B-API`,
`GLM-4.1V-Thinking-FlashX`, `deep-research`, `zero`, `0808-360B-DR`.

`GET /api/config` (public) exposes: `enable_captcha: true`, `enable_mcp: true`,
`enable_artifacts_mode: true`, `enable_websocket: false`, `enable_upload_image: false`,
`default_models: "glm-5.3"`, `completion_version: "2"`, OAuth providers `google/github/maas`, and the MCP
list `deep-web-search`, `exfil-server` ("Internal SSRF", "SSRF test server"), `ppt-maker`.

## 7. What a faithful client must do (checklist)

- [x] mint/persist a guest identity (`/api/v1/auths/`) and keep the JWT
- [x] create the chat before the first turn, keep the user-message uuid it returns
- [x] send **only** the new user message + `chat_id` + parent assistant id
- [x] pass `captcha_verify_param` through when the site asks for it — **never synthesize one**
- [x] treat `done:true` as the only terminator; keep collecting answer deltas after `usage`
- [x] reassemble `tool_call` fragments keyed by the first fragment's `tool_call_id`
- [x] re-fetch `GET /api/v1/chats/<id>` to recover the tree (assistant text stays stubbed)
- [x] expect no cookies; expect the token only in the completions query string

## 8. Limits of this analysis

* Live calls were **not** made from this workspace: egress to `*.z.ai` / `chatglm.cn` is filtered here
  (DNS resolves, TCP fails). Verification therefore replays the **captured bytes** through a local server.
* `x-signature`'s derivation is unknown. In the capture it is a fresh 64-hex value per completion call and is
  not obviously bound to the body (`signature_timestamp` equals `timestamp`). Treat as opaque.
* `captcha_verify_param` is an Aliyun Captcha V3 attestation produced by a JS challenge (`sceneId: didk33e0`).
  This spec documents its *position* in the protocol; nothing here generates or bypasses it.
* Only the guest/`temporary_enforced` tier is captured, on a `preview` model. Logged-in traffic may add
  fields.
* Requests are rate-limited and governed by the site's terms — the replay harness is for offline study, and
  any live use should stay within normal interactive volume.
