"""zai_re.agent — the turn engine: how a prompt becomes a streamed answer.

This encodes the workflow recovered from the capture, phase by phase:

    thinking      decode the (often typo'd) request, plan
    tool_call     search  — arguments streamed as fragments
    tool_response the tool's raw text in the site's own ref_id format
    thinking      analysis of the tool output (what the real model did)
    answer        the visible answer, streamed in small chunks
    other         usage accounting (may land before the final answer chunk)
    done          terminal

The local engine is deliberately *not* an LLM: answers are composed extractively
from the captured corpus, and chat turns use templates. That keeps the protocol
faithful (same frames, same ordering, same timing shape) without pretending a
model is running. For a real model, use `zai_re.live` against the live site.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .corpus import Corpus, Document
from .protocol import EVENT_TYPE
from .typo import Decoded, decode

CHUNK = 46          # characters per delta frame (matches the capture's ~40-90)


def _frames(events: Iterator[dict]) -> Iterator[bytes]:
    for ev in events:
        yield ("data: " + json.dumps({"type": EVENT_TYPE, "data": ev},
                                     ensure_ascii=False) + "\n\n").encode()


def _chunks(text: str, size: int = CHUNK) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


@dataclass
class TurnResult:
    chat_id: str
    prompt: str
    corrected: str
    intent: str
    corrections: list[tuple[str, str, str]] = field(default_factory=list)
    thinking: str = ""
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_responses: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    frames: int = 0
    started: float = 0.0
    finished: float = 0.0

    @property
    def elapsed_ms(self) -> float:
        return round((self.finished - self.started) * 1000, 1)


class Agent:
    """Produces the phase stream for one turn and records what it did."""

    def __init__(self, corpus: Optional[Corpus] = None, *, think_delay: float = 0.012,
                 chunk_delay: float = 0.004) -> None:
        self.corpus = corpus if corpus is not None else Corpus.from_har()
        self.think_delay = think_delay
        self.chunk_delay = chunk_delay

    # ------------------------------------------------------------------ entry
    def run(self, prompt: str, *, chat_id: str = "", history: list[dict] | None = None,
            model: str = "x-preview-l", mcp_servers: list[str] | None = None,
            auto_web_search: bool = False, previous_answer: str = "") -> Iterator[bytes]:
        """Yield SSE bytes; the caller writes them straight to the socket."""
        result = TurnResult(chat_id=chat_id, prompt=prompt, corrected=prompt, intent="chat")
        result.started = time.time()
        self._last = result

        decoded = decode(prompt)
        result.corrected = decoded.text
        result.intent = decoded.intent
        result.corrections = [(c.original, c.fixed, c.kind) for c in decoded.corrections]

        # ---- plan the turn (what decides whether tools fire) ---------------
        wants_search = decoded.intent == "search" or auto_web_search or \
            bool(mcp_servers and "advanced-search" in mcp_servers)

        for ev in self._thinking(self._plan_text(decoded, wants_search)):
            yield _frame(ev); result.frames += 1
        if self.think_delay:
            time.sleep(self.think_delay)

        docs: list[Document] = []
        silent_hits: list[Document] = []
        if not wants_search and decoded.intent == "question":
            # protocol-faithful: no tool frames are emitted for a plain question
            # (the capture shows none for "whats ur name ?"), but the local
            # composer still consults the corpus so the turn can be answered.
            topic = _topic(decoded.text)
            silent_hits = self.corpus.search(topic, limit=4)
        if wants_search:
            for ev in self._tool_search(decoded, result):
                yield _frame(ev); result.frames += 1
            docs = self._last_docs
            for ev in self._thinking(self._analysis_text(decoded, docs)):
                yield _frame(ev); result.frames += 1

        answer = self._compose(decoded, docs or silent_hits, history or [],
                               previous_answer)
        result.answer = answer

        # usage lands before the final answer chunk in the real stream too
        for i, piece in enumerate(_chunks(answer)):
            ev = {"delta_content": piece, "phase": "answer"}
            yield _frame(ev); result.frames += 1
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            if i == max(0, _n_chunks(answer) - 2):        # second-to-last chunk
                usage = self._usage(prompt, result, docs)
                result.usage = usage
                yield _frame({"phase": "other", "usage": usage}); result.frames += 1

        if _n_chunks(answer) < 2:
            result.usage = self._usage(prompt, result, docs)
            yield _frame({"phase": "other", "usage": result.usage}); result.frames += 1

        yield _frame({"phase": "done", "done": True}); result.frames += 1
        result.finished = time.time()

    @property
    def last_result(self) -> TurnResult:
        return self._last

    # ------------------------------------------------------------- phase text
    def _plan_text(self, d: Decoded, wants_search: bool) -> str:
        lines = []
        if d.changed:
            lines.append(f'The user\'s query is garbled: "{d.raw}" — '
                         f'decoding it as "{d.text}".')
            lines.append("Corrections: " + "; ".join(
                f"{o}→{f} ({k})" for o, f, k in
                [(c.original, c.fixed, c.kind) for c in d.corrections]) + ".")
        else:
            lines.append(f'The user asked: "{d.text}".')
        lines.append(f"Intent: {d.intent}. "
                     f"Recognised vocabulary: {int(round(d.confidence * 100))}%.")
        if d.entities:
            lines.append("Entities detected: " + ", ".join(d.entities) + ".")
        if wants_search:
            lines.append("This needs current information, so I'll search first, "
                         "then read the results and compose a cited answer.")
        elif d.intent == "question":
            lines.append("I can answer this from what I already know; no tools needed.")
        else:
            lines.append("Conversational turn — reply directly.")
        return "\n\n".join(lines)

    def _analysis_text(self, d: Decoded, docs: list[Document]) -> str:
        if not docs:
            return ("The search returned nothing usable, so I'll say what I can "
                    "and be explicit about the gap.")
        ages = sorted({doc.date for doc in docs if doc.date})
        doms = sorted({_domain(doc.url) for doc in docs})[:6]
        return (f"Search returned {len(docs)} sources over {len(doms)} domains "
                f"({', '.join(doms)}). Dates seen: {', '.join(ages) or 'unknown'}.\n\n"
                "I'll extract the strongest claims, keep the source markers so the "
                "UI can render citation chips, and note where sources disagree.")

    # ------------------------------------------------------------- tool phase
    _last_docs: list[Document] = []

    def _tool_search(self, d: Decoded, result: TurnResult) -> Iterator[dict]:
        query = re.sub(r"^(deep\s+)?(search|research|browse)\s+", "", d.text, flags=re.I)
        docs = self.corpus.search(query or d.text, limit=6)
        self._last_docs = docs
        call_id = "call_" + "%016x" % random.getrandbits(64)

        args = json.dumps({"search_query": [
            {"q": query.strip()[:80] or d.text[:80], "recency": 14},
            {"q": (query + " latest news").strip()[:80], "recency": 7},
            {"q": (query + " announcement").strip()[:80], "recency": 30},
        ]}, ensure_ascii=False)

        result.tool_calls.append({"name": "search", "id": call_id, "arguments": args})
        # first fragment carries the tool_call_id; later ones only the type
        first, rest = args[:14], args[14:]
        yield {"phase": "tool_call", "delta_name": "search", "delta_arguments": first,
               "metadata": {"tool_call_id": call_id, "type": "function"}}
        for piece in _chunks(rest, 28):
            yield {"phase": "tool_call", "delta_arguments": piece,
                   "metadata": {"type": "function"}}

        rendered = self.corpus.render_results(docs) if docs else "(no results)"
        result.tool_responses.append({"tool_name": "search", "status": "completed",
                                      "content": rendered, "tool_call_id": call_id})
        result.sources = [{"ref_id": doc.ref_id, "title": doc.title, "url": doc.url,
                           "date": doc.date, "score": doc.score} for doc in docs]
        yield {"phase": "tool_response", "tool_name": "search", "status": "completed",
               "delta_content": rendered,
               "metadata": {"tool_call_id": call_id,
                            "browser": {"session_id": call_id, "turn_count": 1}}}

        # a follow-up `open` on the top hit — including the failure path the
        # real agent hit ("Ref id … is invalid")
        if len(docs) >= 2 and random.random() < 1.0:
            open_id = "call_" + "%016x" % random.getrandbits(64)
            target = docs[0]
            open_args = json.dumps({"open": [{"ref_id": target.ref_id, "lineno": 1}]})
            result.tool_calls.append({"name": "open", "id": open_id,
                                      "arguments": open_args})
            yield {"phase": "tool_call", "delta_name": "open",
                   "delta_arguments": open_args,
                   "metadata": {"tool_call_id": open_id, "type": "function"}}
            body = (target.clean_text[:600] if target.clean_text
                    else f"Ref id {target.ref_id} is invalid")
            result.tool_responses.append({"tool_name": "open", "status": "completed",
                                          "content": body, "tool_call_id": open_id})
            yield {"phase": "tool_response", "tool_name": "open",
                   "status": "completed", "delta_content": body,
                   "metadata": {"tool_call_id": open_id,
                                "browser": {"session_id": open_id, "turn_count": 1}}}

    # ----------------------------------------------------------- composition
    def _compose(self, d: Decoded, docs: list[Document],
                 history: list[dict], previous_answer: str) -> str:
        if d.intent == "chat" and not docs:
            low = d.text.lower().strip()
            if re.match(r"^(hey|hi|hello|yo|sup)\b", low):
                return ("Hello! This is the local protocol replica of chat.z.ai's "
                        "chat system. I stream the same frame types the real site "
                        "does — thinking, tool_call, tool_response, answer, usage, "
                        "done — but I compose answers extractively from a captured "
                        "corpus instead of running a model.\n\n"
                        "Ask me something searchable (e.g. \"deep search what's next "
                        "elon musk goals of today\") and you'll see the full tool flow.")
            if "name" in low:
                return ("I'm the local zai_re agent — a faithful re-implementation of "
                        "the chat protocol, not the hosted GLM model. The live model "
                        "answers this by identifying itself as GLM; here you're "
                        "talking to the recovered flow itself.")
            if previous_answer:
                return ("Got it — following on from the previous turn. Ask a question "
                        "or say \"search …\" and I'll run the tool pass.")
            return ("Understood. I run the same turn pipeline as the live site; "
                    "search intents additionally fire the search/open tools.")

        if not docs:
            if history:
                last_user = next((h.get("content", "") for h in reversed(history)
                                  if h.get("role") == "user"), "")
                return (f"Following on from \"{last_user[:70]}\" — the captured corpus "
                        f"has nothing more on \"{_topic(d.text)}\" specifically, and I "
                        "won't invent it. Add \"search\" to the prompt to force a tool "
                        "pass, or use `python3 -m zai_re.live` for the real model from "
                        "a network that can reach chat.z.ai.")
            return ("I don't have sources for that in the captured corpus, and I "
                    "won't invent them. The live site would answer from its model; "
                    "locally, `python3 -m zai_re.live --help` runs the same protocol "
                    "against the real endpoint from a network that can reach it.")

        opener = (f"Here's what the sources say about **{_topic(d.text)}**"
                  if d.intent == "search" else
                  f"From the captured corpus, on **{_topic(d.text)}**")
        lines = [f"{opener}, matching {len(docs)} results.\n"]
        for i, doc in enumerate(docs[:4]):
            head = doc.title.strip() or _domain(doc.url)
            when = f" ({doc.date})" if doc.date else ""
            lines.append(f"- **{head}**{when} — {doc.snippet(200)}"
                         f"【{doc.ref_id}】")
        lines.append("\n**Where sources disagree or are thin:** the captured "
                     "snippets carry navigation junk (nav menus, related links) and "
                     "some entries repeat across queries, so treat repeated claims as "
                     "one source, not several.")
        if d.intent == "search":
            lines.append("\n**TL;DR** — this answer was assembled extractively during "
                         "the tool pass; the live model instead reasons over the same "
                         "tool output (that reasoning is what streams in `thinking`).")
        return "\n".join(lines)

    def _usage(self, prompt: str, result: TurnResult, docs: list[Document]) -> dict:
        prompt_tokens = 14 + len(re.findall(r"\w+", prompt)) * 2
        tool_tokens = sum(len(d.text) for d in docs) // 4
        completion = (len(result.thinking or "") +
                      sum(len(r["content"]) for r in result.tool_responses) +
                      len(result.answer)) // 4
        return {
            "prompt_tokens": prompt_tokens + tool_tokens,
            "completion_tokens": completion,
            "total_tokens": prompt_tokens + tool_tokens + completion,
            "prompt_tokens_details": {"cached_tokens": prompt_tokens // 3},
        }

    def _thinking(self, text: str) -> Iterator[dict]:
        for piece in _chunks(text, 32):
            yield {"delta_content": piece, "phase": "thinking"}
            if self.think_delay:
                time.sleep(self.think_delay * 0.4)


def _frame(ev: dict) -> bytes:
    return ("data: " + json.dumps({"type": EVENT_TYPE, "data": ev},
                                  ensure_ascii=False) + "\n\n").encode()


def _n_chunks(text: str, size: int = CHUNK) -> int:
    return max(1, (len(text) + size - 1) // size)


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else (url or "?")


def _topic(text: str) -> str:
    t = re.sub(r"^(deep\s+)?(search|research|browse)\s+", "", text, flags=re.I)
    t = re.sub(r"^(and|so|but|also|ok|okay|well|then)\b[,:]?\s*", "", t, flags=re.I)
    t = re.sub(r"^(what|how)\s+about\s+", "", t, flags=re.I)
    t = re.sub(r"^(what's|whats|what is|tell me about|tell me)\s+", "", t, flags=re.I)
    return t.strip().rstrip("?.")[:70]
