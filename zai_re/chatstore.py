"""zai_re.chatstore — the server-side chat system (state ownership model).

This is the part of the workflow that is easiest to get wrong and most
interesting: **the server owns the conversation**. Recovered behaviour:

  * the client posts ONE new user message with `chat_id` +
    `current_user_message_parent_id`; it never resends history;
  * the server keeps a linked list of messages — each node has
    `id`, `parentId`, `childrenIds`, `role`, `timestamp`;
  * the *assistant* node stores no `content` in the tree: a follow-up
    `GET /api/v1/chats/<id>` returns it as a stub, which is exactly why the
    browser re-fetches the chat and keeps the streamed text in its own state;
  * two background tasks run off-turn: `title_generation` (seen renaming
    "New Chat" → "Greeting Query") and `tags_generation`.

This module implements that store faithfully, with the option to include
assistant text for debugging (`include_content=True`).
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .typo import decode


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class MessageNode:
    id: str
    parent_id: Optional[str]
    role: str                        # "user" | "assistant"
    content: str = ""                # local copy; not necessarily on the wire
    content_stored: bool = False     # <- faithful: only /chats/new stores text
    children_ids: list[str] = field(default_factory=list)
    timestamp: int = field(default_factory=lambda: int(time.time()))
    models: list[str] = field(default_factory=list)
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def wire(self, include_content: bool = False) -> dict:
        node = {
            "id": self.id,
            "parentId": self.parent_id,
            "childrenIds": list(self.children_ids),
            "role": self.role,
            "timestamp": self.timestamp,
        }
        # Recovered behaviour (verified against the capture):
        #   * the node seeded by POST /api/v1/chats/new carries content;
        #   * a user node created by the *completion* path does NOT (its text
        #     only ever existed in the request body / client state);
        #   * assistant nodes never carry content in the tree.
        if self.content_stored or include_content:
            node["content"] = self.content
        if self.role == "user":
            node["models"] = self.models
        return node


@dataclass
class ChatRecord:
    id: str
    title: str = "New Chat"
    models: list[str] = field(default_factory=list)
    messages: dict[str, MessageNode] = field(default_factory=dict)
    current_id: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    tags: list[str] = field(default_factory=list)
    enable_thinking: bool = True
    reasoning_effort: str = "max"
    auto_web_search: bool = False
    mcp_servers: list[str] = field(default_factory=list)
    user_id: str = "local-user"

    # -------------------------------------------------------------- tree api
    def add_user(self, content: str, model: str, *, msg_id: Optional[str] = None,
                 parent_id: Optional[str] = None,
                 store_content: bool = True) -> MessageNode:
        """Add (or re-attach) a user node.

        `msg_id` mirrors `current_user_message_id` from the completion body: if
        it already exists the node is reused, which is exactly how the live site
        avoids duplicating turn 1 (seeded by /chats/new, then referenced by the
        first completion).
        """
        if msg_id and msg_id in self.messages:
            node = self.messages[msg_id]
            self.current_id = node.id
            return node
        parent = parent_id if parent_id is not None else self.current_id
        if parent and parent not in self.messages:
            parent = self.current_id
        node = MessageNode(id=msg_id or _uuid(), parent_id=parent, role="user",
                           content=content, models=[model],
                           content_stored=store_content)
        if parent and parent in self.messages:
            self.messages[parent].children_ids.append(node.id)
        self.messages[node.id] = node
        self.current_id = node.id
        self.updated_at = int(time.time())
        return node

    def add_assistant(self, parent_id: str, content: str, *, reasoning: str = "",
                      tool_calls: list[dict] | None = None,
                      usage: dict | None = None, model: str = "") -> MessageNode:
        node = MessageNode(id=_uuid(), parent_id=parent_id, role="assistant",
                           content=content, reasoning=reasoning,
                           tool_calls=tool_calls or [], usage=usage or {},
                           models=[model] if model else [])
        if parent_id in self.messages:
            self.messages[parent_id].children_ids.append(node.id)
        self.messages[node.id] = node
        self.current_id = node.id
        self.updated_at = int(time.time())
        return node

    def path(self) -> list[MessageNode]:
        """Walk parentId links from currentId back to the root."""
        out: list[MessageNode] = []
        node_id = self.current_id
        while node_id and node_id in self.messages:
            node = self.messages[node_id]
            out.append(node)
            node_id = node.parent_id
        return list(reversed(out))

    def history(self) -> list[dict]:
        return [{"role": n.role, "content": n.content} for n in self.path()]

    # ------------------------------------------------------------- wire forms
    def wire(self, include_content: bool = False) -> dict:
        return {
            "id": self.id, "user_id": self.user_id, "title": self.title,
            "chat": {
                "id": self.id, "models": self.models, "params": {},
                "history": {
                    "messages": {mid: m.wire(include_content)
                                 for mid, m in self.messages.items()},
                    "currentId": self.current_id,
                },
                "tags": self.tags,
                "features": [{"server": "tool_selector_h", "status": "hidden",
                              "type": "tool_selector"}],
                "enable_thinking": self.enable_thinking,
                "reasoning_effort": self.reasoning_effort,
                "auto_web_search": self.auto_web_search,
                "message_version": 1, "extra": {},
            },
            "updated_at": self.updated_at, "created_at": self.created_at,
            "share_id": None, "archived": False, "pinned": False,
            "meta": {"auto_web_search": self.auto_web_search, "flags": None,
                     "mcp_servers": self.mcp_servers, "models": self.models,
                     "workspace_id": self.id},
            "folder_id": None, "message_version": 1, "type": "default",
            "im_context": None,
        }

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "updated_at": self.updated_at,
                "created_at": self.created_at, "models": self.models,
                "tags": self.tags,
                "messages": len(self.messages)}


class ChatStore:
    """In-memory store + the two background tasks the real server runs."""

    def __init__(self, title_delay: float = 0.0, tags_delay: float = 0.0) -> None:
        self.chats: dict[str, ChatRecord] = {}
        self.lock = threading.RLock()
        self.title_delay = title_delay
        self.tags_delay = tags_delay
        self.background_log: list[dict] = []

    # ------------------------------------------------------------------ crud
    def create(self, prompt: str = "", model: str = "x-preview-l",
               chat_id: str = "", title: str = "New Chat",
               enable_thinking: bool = True, reasoning_effort: str = "max",
               msg_id: Optional[str] = None) -> ChatRecord:
        rec = ChatRecord(id=chat_id or _uuid(), title=title, models=[model],
                         enable_thinking=enable_thinking,
                         reasoning_effort=reasoning_effort)
        if prompt:
            # the id the client used in the /chats/new payload is the id it will
            # later reference as current_user_message_id — keep them identical
            rec.add_user(prompt, model, msg_id=msg_id)
        with self.lock:
            self.chats[rec.id] = rec
        return rec

    def get(self, chat_id: str) -> Optional[ChatRecord]:
        return self.chats.get(chat_id)

    def list(self) -> list[dict]:
        return [c.summary() for c in
                sorted(self.chats.values(), key=lambda c: -c.updated_at)]

    def delete(self, chat_id: str) -> bool:
        with self.lock:
            return self.chats.pop(chat_id, None) is not None

    # ---------------------------------------------------- background workers
    def schedule_background(self, chat: ChatRecord, prompt: str,
                            *, title: bool = True, tags: bool = True) -> None:
        """Faithful stand-in for `background_tasks:{title_generation,tags_generation}`."""
        if title:
            threading.Thread(target=self._gen_title, args=(chat, prompt),
                             daemon=True).start()
        if tags:
            threading.Thread(target=self._gen_tags, args=(chat, prompt),
                             daemon=True).start()

    def _gen_title(self, chat: ChatRecord, prompt: str) -> None:
        if self.title_delay:
            time.sleep(self.title_delay)
        title = self.make_title(prompt)
        with self.lock:
            if chat.id in self.chats and chat.title in ("New Chat", ""):
                old = chat.title
                chat.title = title
                chat.updated_at = int(time.time())
                self.background_log.append({
                    "task": "title_generation", "chat_id": chat.id,
                    "from": old, "to": title, "at": time.time()})

    def _gen_tags(self, chat: ChatRecord, prompt: str) -> None:
        if self.tags_delay:
            time.sleep(self.tags_delay)
        d = decode(prompt)
        tags = list(dict.fromkeys(d.entities + [d.intent]))[:3]
        with self.lock:
            if chat.id in self.chats:
                chat.tags = tags
                self.background_log.append({
                    "task": "tags_generation", "chat_id": chat.id,
                    "tags": tags, "at": time.time()})

    # --------------------------------------------------------------- titling
    @staticmethod
    def make_title(prompt: str) -> str:
        """Heuristic mirroring the observed 'Greeting Query' style titles."""
        d = decode(prompt)
        text = d.text.strip().rstrip("?.!")
        low = text.lower()
        if re.match(r"^(hey|hi|hello|yo|sup)\b", low):
            return "Greeting Query"
        if d.intent == "search":
            topic = re.sub(r"^(deep\s+)?(search|research|browse)\s+", "", low)
            topic = re.sub(r"^(what's|whats|what is)\s+", "", topic)
            words = topic.split()[:5]
            return ("Search: " + " ".join(words)).strip()[:60].title()
        words = text.split()[:6]
        return (" ".join(words)[:60] or "New Chat").strip().capitalize()
