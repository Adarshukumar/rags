"""zai_re.server_chat — a working chatbot that speaks chat.z.ai's protocol.

The whole recovered turn loop, running locally and end to end:

    browser UI  ──POST /api/v1/chats/new──▶  server-side chat store (linked list)
                ──POST /api/v2/chat/completions──▶  agent
                      ◀── SSE: thinking → tool_call → tool_response → answer
                               → usage → done ──
                ──GET  /api/v1/chats/<id>──▶  tree (assistant text stubbed, as live)
                ◀──  background_tasks: title_generation, tags_generation ──

Run:
    python3 -m zai_re.server_chat --port 8802          # UI at http://localhost:8802/
    python3 -m zai_re.server_chat --port 8802 --think-delay 0.02 --chunk-delay 0.01

This is the protocol replica: answers are composed extractively from the corpus
captured in chat.z.ai.har, and no language model runs here. For the live model,
use `python3 -m zai_re.live`.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .agent import Agent
from .chatstore import ChatStore
from .corpus import Corpus
from .har_source import HarSource
from .typo import decode

# --------------------------------------------------------------------------- UI
CHAT_UI = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>zai_re — chat.z.ai protocol replica</title>
<style>
:root{color-scheme:dark;--bg:#0b0f14;--panel:#111820;--panel2:#0e141b;--line:#1e2833;
      --txt:#e6edf3;--dim:#8b98a5;--accent:#4c9aff;--green:#3fb950;--amber:#d29922}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;background:var(--bg);color:var(--txt);
     font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
/* sidebar */
aside{width:250px;flex:0 0 250px;border-right:1px solid var(--line);display:flex;
      flex-direction:column;background:var(--panel2)}
.logo{padding:14px 14px 10px;font-weight:650;letter-spacing:.2px;font-size:13.5px}
.logo small{display:block;font-weight:400;color:var(--dim);font-size:11px;margin-top:3px}
#newchat{margin:0 12px 10px;background:var(--accent);color:#04101f;border:0;
         border-radius:8px;padding:9px;font:inherit;font-weight:600;cursor:pointer}
#chats{flex:1;overflow:auto;padding:0 8px 12px}
.chatitem{padding:8px 10px;border-radius:7px;cursor:pointer;color:var(--dim);
          font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
          display:flex;gap:6px;align-items:center}
.chatitem:hover{background:#161f29;color:var(--txt)}
.chatitem.on{background:#1b2530;color:var(--txt)}
.chatitem b{font-weight:500;overflow:hidden;text-overflow:ellipsis}
.chatitem i{opacity:0;margin-left:auto;font-style:normal;color:var(--dim);padding:0 3px}
.chatitem:hover i{opacity:1}
.sidefoot{padding:9px 12px;border-top:1px solid var(--line);color:var(--dim);font-size:11px}
/* main */
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{padding:11px 18px;border-bottom:1px solid var(--line);display:flex;gap:10px;
       align-items:center;flex-wrap:wrap;background:var(--panel)}
header h1{font-size:14px;margin:0;font-weight:600}
.badge{font-size:10.5px;border:1px solid var(--line);border-radius:99px;padding:2px 8px;
       color:var(--dim);letter-spacing:.3px}
.badge.replica{border-color:#3a2f12;background:#221a08;color:var(--amber)}
.spacer{flex:1}
.toggle{font-size:11.5px;color:var(--dim);cursor:pointer;user-select:none}
.toggle input{margin-right:5px;vertical-align:-1px}
#stream{flex:1;overflow:auto;padding:22px 22px 8px}
.msg{max-width:860px;margin:0 auto 22px}
.msg.user{display:flex;justify-content:flex-end}
.bubble{background:#1b2530;padding:10px 14px;border-radius:12px 12px 3px 12px;
        max-width:78%;white-space:pre-wrap}
.fixes{margin:6px 2px 0;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.fix{font-size:11px;color:var(--dim);border:1px dotted var(--line);border-radius:6px;
     padding:1px 6px}
.fix s{color:#7d3f3f}.fix b{color:var(--green);font-weight:500}
.assistant .who{font-size:11px;color:var(--dim);margin-bottom:6px;display:flex;gap:8px;
                align-items:center;flex-wrap:wrap}
details.think{border-left:2px solid var(--line);padding-left:11px;margin:0 0 12px;color:var(--dim);
     font-size:12.5px}
details.think summary{cursor:pointer;color:var(--dim);font-size:11.5px;outline:none}
details.think pre{white-space:pre-wrap;margin:8px 0 0;font:inherit;color:#96a3b0}
.tool{border:1px solid var(--line);border-radius:9px;margin:0 0 10px;overflow:hidden;
      background:var(--panel2)}
.tool .th{padding:7px 11px;display:flex;gap:9px;align-items:center;font-size:12px;
          cursor:pointer;color:var(--dim)}
.tool .th b{color:var(--txt);font-weight:600;font-size:12px}
.tool .tb{border-top:1px solid var(--line);padding:10px 12px;font-size:12px;color:var(--dim);
          max-height:230px;overflow:auto;white-space:pre-wrap;display:none}
.tool.open .tb{display:block}
.pill{font-size:10px;border:1px solid var(--line);border-radius:99px;padding:1px 7px}
.pill.ok{border-color:#1c3a24;color:var(--green)}
.answer{white-space:pre-wrap}
.answer h3{margin:14px 0 6px;font-size:14px}
.answer strong{color:#fff}
.answer code{background:#161f29;padding:1px 5px;border-radius:4px;font-size:12.5px}
.cite{display:inline-block;font-size:10.5px;color:var(--accent);border:1px solid #1d3552;
      background:#0e1c2c;border-radius:5px;padding:0 5px;margin:0 1px;cursor:help}
.sources{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.sources .st{font-size:11px;color:var(--dim);margin-bottom:7px}
.srclist{display:flex;flex-direction:column;gap:5px}
.src{font-size:11.5px;color:var(--dim);display:flex;gap:7px;align-items:baseline}
.src em{font-style:normal;color:var(--accent);font-size:10.5px}
.src a{color:var(--txt);text-decoration:none;border-bottom:1px dotted var(--line)}
.src span{color:var(--dim)}
.usage{font-size:11px;color:var(--dim);margin-top:9px}
.cursor{display:inline-block;width:7px;height:14px;background:var(--accent);
        vertical-align:-2px;animation:b .9s steps(2) infinite}
@keyframes b{50%{opacity:0}}
/* composer */
footer{border-top:1px solid var(--line);padding:12px 22px 16px;background:var(--panel)}
.composer{max-width:860px;margin:0 auto;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;background:var(--panel2);color:var(--txt);border:1px solid var(--line);
         border-radius:10px;padding:11px 13px;font:inherit;resize:none;min-height:46px;
         max-height:170px}
textarea:focus{outline:none;border-color:#2d4a6b}
#send{background:var(--accent);color:#04101f;border:0;border-radius:9px;padding:12px 17px;
      font:inherit;font-weight:650;cursor:pointer}
#send:disabled{opacity:.45;cursor:default}
.hint{max-width:860px;margin:8px auto 0;font-size:11px;color:var(--dim);display:flex;
      gap:12px;flex-wrap:wrap}
.kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:10.5px}
/* inspector */
#inspector{width:330px;flex:0 0 330px;border-left:1px solid var(--line);display:none;
           flex-direction:column;background:var(--panel2)}
#inspector.show{display:flex}
#inspector h2{font-size:12px;margin:0;padding:11px 14px;border-bottom:1px solid var(--line);
              font-weight:600;color:var(--dim);letter-spacing:.3px}
#counts{display:flex;gap:6px;flex-wrap:wrap;padding:11px 14px;border-bottom:1px solid var(--line)}
#rawlog{flex:1;overflow:auto;padding:10px 12px;font:11.5px/1.55 ui-monospace,Menlo,monospace;
        color:var(--dim);white-space:pre-wrap}
#rawlog .ph-thinking{color:#7f8b99}
#rawlog .ph-answer{color:#9fd0ff}
#rawlog .ph-tool_call{color:#e3b341}
#rawlog .ph-tool_response{color:#7ee787}
#rawlog .ph-done{color:#ff7b72}
</style></head><body>
<aside>
  <div class="logo">zai_re chat<small>chat.z.ai protocol replica</small></div>
  <button id="newchat">+ New chat</button>
  <div id="chats"></div>
  <div class="sidefoot">server-side history · linked-list tree<br>
    answers: extractive over captured corpus</div>
</aside>
<main>
  <header>
    <h1 id="title">New Chat</h1>
    <span class="badge replica">LOCAL REPLICA</span>
    <span class="badge" id="modelbadge">x-preview-l</span>
    <span class="spacer"></span>
    <label class="toggle"><input type="checkbox" id="deepsearch">force deep search</label>
    <label class="toggle"><input type="checkbox" id="showraw">show protocol</label>
  </header>
  <div id="stream"></div>
  <footer>
    <div class="composer">
      <textarea id="input" rows="1"
        placeholder="Ask anything — typos welcome. Try: deep search whats next elonmkusk goad of towdy"></textarea>
      <button id="send">Send</button>
    </div>
    <div class="hint">
      <span><span class="kbd">Enter</span> send · <span class="kbd">Shift+Enter</span> newline</span>
      <span id="stat">ready</span>
      <span>frames: <b id="framecount">0</b></span>
    </div>
  </footer>
</main>
<aside id="inspector">
  <h2>PROTOCOL INSPECTOR</h2>
  <div id="counts"></div>
  <div id="rawlog"></div>
</aside>
<script>
const $ = id => document.getElementById(id);
let chatId = null, sending = false, frames = 0, counts = {};
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function md(t){
  let h = esc(t);
  h = h.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/【(turn\d+search\d+)】/g, '<span class="cite" title="source $1">$1</span>');
  return h;
}
function bubble(kind, text){ const d=document.createElement('div');
  d.className='msg '+kind; d.innerHTML = kind==='user' ? esc(text) : md(text);
  return d; }

async function api(path, opts){ const r = await fetch(path, opts);
  if(!r.ok) throw new Error(path+' → '+r.status); return r.json(); }

async function refreshChats(){
  const list = await api('/api/v1/chats');
  $('chats').innerHTML = '';
  list.forEach(c => {
    const el = document.createElement('div');
    el.className = 'chatitem' + (c.id===chatId ? ' on':'');
    el.innerHTML = `<b>${esc(c.title||'New Chat')}</b><i title="delete">✕</i>`;
    el.onclick = e => { if(e.target.tagName==='I'){ del(c.id); } else { openChat(c.id); } };
    $('chats').appendChild(el);
  });
}
async function del(id){ await fetch('/api/v1/chats/'+id,{method:'DELETE'});
  if(id===chatId){ chatId=null; $('stream').innerHTML=''; $('title').textContent='New Chat'; }
  refreshChats(); }
async function openChat(id){ chatId = id; const rec = await api('/api/v1/chats/'+id);
  $('title').textContent = rec.title; $('stream').innerHTML = '';
  const nodes = rec.chat.history.messages;
  // faithful quirk: assistant text is NOT in the tree (server stubs it), so a
  // reloaded chat shows the user turns only — exactly like the live site.
  Object.values(nodes).forEach(n => { if(n.role==='user')
      $('stream').appendChild(bubble('user', n.content||'')); });
  if(Object.values(nodes).some(n=>n.role==='assistant')){
    const note = bubble('assistant',
      '_Assistant text is stubbed in the server tree (that is the real behaviour) — ' +
      'this replica keeps its own copy while the tab is open._');
    $('stream').appendChild(note); }
  refreshChats(); }

function toolCard(name, args, body, chars){
  const d = document.createElement('div'); d.className='tool';
  d.innerHTML = `<div class="th"><b>${esc(name)}</b>
      <span class="pill">args streamed as fragments</span>
      <span class="spacer" style="flex:1"></span>
      <span>${body!==undefined? (chars+' chars'):'running…'}</span></div>
    <div class="tb">${esc(args||'')}${body!==undefined?'\n\n'+esc(body):''}</div>`;
  d.querySelector('.th').onclick = () => d.classList.toggle('open');
  return d;
}

async function send(){
  const text = $('input').value.trim(); if(!text || sending) return;
  sending = true; $('send').disabled = true; $('input').value='';
  frames = 0; counts = {}; renderCounts();

  const card = document.createElement('div');
  card.className = 'msg user';
  const wrap = document.createElement('div');
  wrap.appendChild(bubble('user', text));
  const fixRow = document.createElement('div'); fixRow.className='fixes';
  wrap.appendChild(fixRow);
  card.appendChild(wrap); $('stream').appendChild(card);

  if(!chatId){
    const rec = await api('/api/v1/chats/new', {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({chat:{title:'New Chat', models:['x-preview-l'],
        history:{messages:{}}}})});
    chatId = rec.id; $('title').textContent = rec.title;
  }

  const msg = document.createElement('div'); msg.className='msg assistant';
  const who = document.createElement('div'); who.className='who';
  who.innerHTML = '<span>GLM (replica)</span><span class="badge">x-preview-l</span>';
  msg.appendChild(who);
  const think = document.createElement('details'); think.className='think';
  think.innerHTML = '<summary>thinking…</summary><pre></pre>';
  const thinkBody = think.querySelector('pre');
  msg.appendChild(think);
  const answer = document.createElement('div'); answer.className='answer';
  msg.appendChild(answer);
  const usageEl = document.createElement('div'); usageEl.className='usage';
  msg.appendChild(usageEl);
  $('stream').appendChild(msg);
  scroll();

  let answerText = '', thinkText = '', toolEl = null, sources = [];
  const body = {stream:true, model:'x-preview-l', signature_prompt:text,
    messages:[{role:'user', content:text}], chat_id: chatId,
    features:{auto_web_search: $('deepsearch').checked, enable_thinking:true,
              reasoning_effort:'max'},
    mcp_servers: $('deepsearch').checked ? ['advanced-search'] : []};

  try{
    const r = await fetch('/api/v2/chat/completions', {method:'POST',
      headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf='';
    while(true){
      const {value, done} = await reader.read(); if(done) break;
      buf += dec.decode(value, {stream:true});
      let nl;
      while((nl = buf.indexOf('\n')) >= 0){
        const line = buf.slice(0,nl).trim(); buf = buf.slice(nl+1);
        if(!line.startsWith('data:')) continue;
        const ev = JSON.parse(line.slice(5).trim()); const d = ev.data||{};
        frames++; counts[d.phase||(d.done?'done':'?')] =
          (counts[d.phase||(d.done?'done':'?')]||0)+1;
        logFrame(d);
        if(d.phase==='thinking' && d.delta_content){ thinkText += d.delta_content;
          thinkBody.textContent = thinkText; }
        if(d.phase==='answer' && d.delta_content){ answerText += d.delta_content;
          answer.innerHTML = md(answerText) + '<span class="cursor"></span>'; }
        if(d.phase==='tool_call' && d.delta_name){
          toolEl = toolCard(d.delta_name, d.delta_arguments||'', undefined);
          msg.insertBefore(toolEl, answer); }
        else if(d.phase==='tool_call' && toolEl){ toolEl.querySelector('.tb').textContent
          += d.delta_arguments||''; }
        if(d.phase==='tool_response'){
          const tb = toolEl.querySelector('.tb');
          tb.textContent += '\n\n── tool_response (' + (d.delta_content||'').length + ' chars)\n'
            + (d.delta_content||''); toolEl.querySelector('.th span:last-child').textContent
            = (d.delta_content||'').length + ' chars';
          (d.delta_content||'').replace(/\[ref_id=(turn\d+search\d+)†([^†]+)†([^\]]+)\]/g,
            (m,ref,title,url) => { sources.push({ref,title,url}); return m; });
        }
        if(d.usage){ usageEl.textContent = 'usage: ' +
          Object.entries(d.usage).map(([k,v]) =>
            k==='prompt_tokens_details' ? `cached=${v.cached_tokens||0}` :
            `${k}=${v}`).join(' · '); }
        renderCounts(); scroll();
      }
    }
  }catch(e){ answer.innerHTML += '<div style="color:#ff7b72">[stream error] '+esc(e.message)+'</div>'; }

  answer.innerHTML = md(answerText);
  if(sources.length){
    const s = document.createElement('div'); s.className='sources';
    s.innerHTML = '<div class="st">sources — from the captured tool_response frames</div>'
      + '<div class="srclist">' + sources.map(x =>
        `<div class="src"><em>${esc(x.ref)}</em><a href="${esc(x.url)}" target="_blank"
          rel="noopener">${esc(x.title||x.url)}</a><span>${esc(x.url)}</span></div>`).join('')
      + '</div>';
    msg.appendChild(s);
  }
  think.open = false;
  think.querySelector('summary').textContent =
    `thinking · ${thinkText.length} chars (${counts.thinking||0} frames)`;
  sending = false; $('send').disabled = false; $('input').focus();
  // the server's background task rewrites "New Chat" → a real title shortly after
  setTimeout(async () => { if(chatId){ const rec = await api('/api/v1/chats/'+chatId);
    $('title').textContent = rec.title; refreshChats(); } }, 350);
}
function scroll(){ $('stream').scrollTop = $('stream').scrollHeight; }
function renderCounts(){ $('framecount').textContent = frames;
  $('counts').innerHTML = Object.entries(counts).map(([k,v]) =>
    `<span class="pill ${k==='done'?'ok':''}">${k} ${v}</span>`).join(''); }
function logFrame(d){
  const ph = d.phase || (d.done?'done':'?');
  const line = document.createElement('div'); line.className = 'ph-'+ph;
  let txt = ph;
  if(d.delta_name) txt += ' → ' + d.delta_name;
  if(d.delta_arguments) txt += ' ' + JSON.stringify(d.delta_arguments);
  if(d.delta_content) txt += ' ' + JSON.stringify(d.delta_content.slice(0,90));
  if(d.tool_name) txt += ' ← ' + d.tool_name + ' (' + (d.delta_content||'').length + 'B)';
  if(d.usage) txt += ' ' + JSON.stringify(d.usage);
  line.textContent = txt; $('rawlog').appendChild(line);
}

$('send').onclick = send;
$('input').addEventListener('keydown', e => {
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }});
$('input').addEventListener('input', e => { e.target.style.height='auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 170)+'px'; });
$('newchat').onclick = () => { chatId=null; $('stream').innerHTML='';
  $('title').textContent='New Chat'; $('rawlog').innerHTML=''; refreshChats(); };
$('showraw').onchange = e => $('inspector').classList.toggle('show', e.target.checked);
refreshChats();
</script></body></html>
"""


# ------------------------------------------------------------------------ server
class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store: ChatStore = None      # type: ignore
    agent: Agent = None          # type: ignore
    source: HarSource = None     # type: ignore

    def log_message(self, fmt, *args):
        if "--verbose" in __import__("sys").argv:
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------
    def _send(self, body: bytes, status=200, ctype="application/json",
              extra: Optional[dict] = None):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), status)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path == "/":
            return self._send(CHAT_UI.encode(), ctype="text/html; charset=utf-8")
        if path == "/health":
            return self._json({"ok": True, "chats": len(self.store.chats),
                               "corpus_docs": len(self.agent.corpus.docs),
                               "background": self.store.background_log[-6:],
                               "mode": "local-replica"})
        if path == "/api/v1/auths/":
            return self._json({
                "id": "local-user", "email": "local@replica", "name": "Local Replica",
                "role": "local", "token": "local-replica-token-not-a-credential",
                "token_type": "Bearer", "expires_at": None,
                "permissions": {"chat": {"temporary": True, "temporary_enforced": True},
                                "features": {"web_search": True}}})
        if path == "/api/config":
            return self._json(self.source.config_payload())
        if path == "/api/models":
            return self._json(self.source.models_payload())
        if path.startswith("/api/v1/scene-cfg/"):
            return self._json({"code": 0, "msg": "success", "data": [
                {"namespace": "zai", "model": "x-preview-l", "scene": "replica",
                 "options": {}}]})
        if path == "/api/v1/chats":
            return self._json(self.store.list())
        if path.startswith("/api/v1/chats/"):
            chat_id = path.rsplit("/", 1)[-1]
            rec = self.store.get(chat_id)
            if rec is None:
                return self._json({"detail": "Chat not found"}, 404)
            include = qs.get("include_content", ["0"])[0] in ("1", "true")
            return self._json(rec.wire(include_content=include))
        return self._json({"detail": "Not Found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/api/v1/chats/"):
            chat_id = urlparse(self.path).path.rsplit("/", 1)[-1]
            return self._json({"deleted": self.store.delete(chat_id)})
        return self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/v1/chats/new":
            return self._chat_new()
        if path == "/api/v2/chat/completions":
            return self._completions()
        return self._json({"detail": "Not Found"}, 404)

    def _chat_new(self):
        body = self._body().get("chat", {})
        models = body.get("models") or ["x-preview-l"]
        history = (body.get("history") or {}).get("messages") or {}
        prompt, msg_id = "", None
        for node in history.values():
            if node.get("role") == "user":
                prompt, msg_id = node.get("content", ""), node.get("id")
        rec = self.store.create(prompt=prompt, model=models[0],
                                title=body.get("title", "New Chat"),
                                enable_thinking=body.get("enable_thinking", True),
                                reasoning_effort=body.get("reasoning_effort", "max"),
                                msg_id=msg_id)
        self._json(rec.wire())

    def _completions(self):
        body = self._body()
        chat_id = body.get("chat_id", "")
        rec = self.store.get(chat_id) if chat_id else None
        if rec is None:
            rec = self.store.create(prompt="", model=body.get("model", "x-preview-l"))
            chat_id = rec.id

        prompt = (body.get("signature_prompt")
                  or next((m.get("content", "") for m in body.get("messages", [])
                           if m.get("role") == "user"), ""))
        features = body.get("features", {}) or {}
        mcp = body.get("mcp_servers") or []

        # ---- state ownership: the server stitches the tree by id ------------
        # verified in the capture: turn 1 reuses the id seeded by /chats/new
        # (content stored); later turns reference a NEW user id and the server
        # creates that node WITHOUT content — the text stays in the request body
        # and in the client's own state.
        user_msg_id = body.get("current_user_message_id") or None
        user_parent = body.get("current_user_message_parent_id")
        seeded = bool(user_msg_id and user_msg_id in rec.messages)
        if user_parent:
            rec.current_id = user_parent
        user_node = rec.add_user(prompt, body.get("model", "x-preview-l"),
                                 msg_id=user_msg_id, parent_id=user_parent,
                                 store_content=seeded)

        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-trace-id", uuid.uuid4().hex[:16])
        self.send_header("access-control-allow-origin", "*")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        agent = self.agent
        try:
            history = [{"role": n.role, "content": n.content} for n in rec.path()]
            for frame in agent.run(
                    prompt, chat_id=chat_id, history=history,
                    mcp_servers=mcp,
                    auto_web_search=bool(features.get("auto_web_search"))):
                self._chunk(frame)
            self._chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            return

        # ---- after the stream: persist the assistant node + run background jobs
        res = agent.last_result
        rec.add_assistant(user_node.id, res.answer, reasoning=res.thinking or "",
                          tool_calls=res.tool_calls, usage=res.usage,
                          model=body.get("model", "x-preview-l"))
        bg = body.get("background_tasks", {"title_generation": True,
                                           "tags_generation": True})
        self.store.schedule_background(rec, prompt,
                                       title=bool(bg.get("title_generation", True)),
                                       tags=bool(bg.get("tags_generation", True)))

    def _chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()


def serve(port: int = 8802, host: str = "0.0.0.0", *, think_delay: float = 0.012,
          chunk_delay: float = 0.004, har: Optional[str] = None,
          title_delay: float = 0.25) -> ThreadingHTTPServer:
    source = HarSource(har) if har else HarSource()
    corpus = Corpus.from_har(har)
    store = ChatStore(title_delay=title_delay)
    agent = Agent(corpus, think_delay=think_delay, chunk_delay=chunk_delay)
    handler = type("BoundChatHandler", (ChatHandler,),
                   {"store": store, "agent": agent, "source": source})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="chat.z.ai protocol replica — chatbot server")
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--think-delay", type=float, default=0.012)
    ap.add_argument("--chunk-delay", type=float, default=0.004)
    ap.add_argument("--har", default=None)
    args = ap.parse_args()

    httpd = serve(args.port, args.host, think_delay=args.think_delay,
                  chunk_delay=args.chunk_delay, har=args.har)
    h = httpd.RequestHandlerClass  # type: ignore[attr-defined]
    print(f"[zai_re.chat] chatbot on http://{args.host}:{args.port}")
    print(f"[zai_re.chat] corpus: {len(h.agent.corpus.docs)} captured documents")
    print(f"[zai_re.chat] protocol: POST /api/v2/chat/completions → SSE "
          f"(thinking → tool_call → tool_response → answer → usage → done)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[zai_re.chat] bye")


if __name__ == "__main__":
    main()
