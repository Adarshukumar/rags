"""zai_re.corpus — the searchable corpus recovered from the capture.

The HAR contains the *actual* tool output the live agent received: four
`tool_response` frames holding 42 source documents in the site's own format:

    [ref_id=turn0search0†Title†https://domain]
    Date: Jul 9, 2026
    …text…

This module parses those frames into documents and gives the local agent a real
search tool over them — so a deep-search turn in the replica retrieves real
captured content instead of invented text.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .har_source import HarSource
from .protocol import CompletionStream, Event, iter_sse_events

DOC_HEADER = re.compile(r"\[ref_id=([^†\]]+)†([^†\]]*)†([^\]]+)\]")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "at", "by", "from", "as", "that", "this",
    "it", "its", "he", "she", "they", "his", "her", "their", "we", "you", "i",
    "will", "has", "have", "had", "not", "but", "up", "out", "about", "after",
    "more", "than", "then", "so", "if", "into", "over", "very", "can", "could",
}


@dataclass
class Document:
    ref_id: str
    title: str
    url: str
    date: str = ""
    text: str = ""
    tool_call_id: str = ""
    score: float = 0.0
    match_query: str = ""

    @property
    def clean_text(self) -> str:
        # the site's snippets carry nav junk ("- Facebook - Twitter - …"); trim it
        t = re.sub(r"\s+", " ", self.text).strip()
        t = re.sub(r"^(?:- [^-]{0,40}){3,}", "", t).strip()
        return t

    def snippet(self, chars: int = 320, query: str | None = None) -> str:
        """Best extract: the window with the most query terms, else the lead."""
        t = self.clean_text
        if len(t) <= chars:
            return t
        q = [w for w in TOKEN_RE.findall((query or self.match_query or "").lower())
             if w not in STOPWORDS and len(w) > 2]
        if q:
            best, best_hits = 0, -1
            step = max(chars // 2, 1)
            for start in range(0, max(1, len(t) - chars), step):
                window = t[start:start + chars].lower()
                hits = sum(window.count(term) for term in q)
                if hits > best_hits:
                    best, best_hits = start, hits
            cut = t[best:best + chars]
            if best > 0:
                cut = "… " + cut
            stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            return (cut[:stop + 1] if stop > chars * 0.55 else cut.rstrip() + " …")
        cut = t[:chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        return (cut[:stop + 1] if stop > chars * 0.5 else cut.rstrip() + " …")


class Corpus:
    """Documents + a tiny TF-IDF searcher (stdlib only)."""

    def __init__(self, docs: list[Document]) -> None:
        self.docs = docs
        self.by_ref = {d.ref_id: d for d in docs}
        self._df: Counter = Counter()
        for d in docs:
            self._df.update(set(self._tokens(d.title + " " + d.text)))
        self._n = max(len(docs), 1)

    # -------------------------------------------------------------- building
    @classmethod
    def from_har(cls, path: str | None = None) -> "Corpus":
        src = HarSource(path) if path else HarSource()
        docs: list[Document] = []
        for stream in src.streams():
            state = CompletionStream()
            for obj in iter_sse_events(stream.raw_sse):
                state.feed(Event.from_wire(obj))
            for resp in state.tool_responses:
                if resp.get("tool_name") != "search":
                    continue
                docs.extend(cls._parse_search_response(
                    resp.get("content", ""),
                    (resp.get("metadata") or {}).get("tool_call_id", "")))
        # IMPORTANT: ref ids restart at turn0search0 for every search call
        # (that is also why the live agent's `open` failed on "turn0search10/
        # 12 is invalid"). So de-duplicate on the URL, not the ref id, and keep
        # the capture's ref id as informational metadata.
        seen: dict[str, Document] = {}
        for d in docs:
            key = re.sub(r"^https?://(www\.)?", "", d.url.lower()).rstrip("/")
            cur = seen.get(key)
            if cur is None or len(d.text) > len(cur.text):
                seen[key] = d
        return cls(list(seen.values()))

    @staticmethod
    def _parse_search_response(text: str, tool_call_id: str) -> list[Document]:
        docs: list[Document] = []
        matches = list(DOC_HEADER.finditer(text))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end():end].strip()
            date = ""
            dm = re.match(r"Date:\s*([^\n]+)", body)
            if dm:
                date = dm.group(1).strip()
                body = body[dm.end():].strip()
            docs.append(Document(
                ref_id=m.group(1).strip(), title=m.group(2).strip(),
                url=m.group(3).strip(), date=date, text=body,
                tool_call_id=tool_call_id))
        return docs

    # -------------------------------------------------------------- searching
    @staticmethod
    def _tokens(s: str) -> list[str]:
        return [w for w in TOKEN_RE.findall(s.lower())
                if w not in STOPWORDS and len(w) > 1]

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n + 1) / (df + 1)) + 1.0

    def search(self, query: str, limit: int = 6) -> list[Document]:
        q = self._tokens(query)
        if not q:
            return []
        scored: list[Document] = []
        for d in self.docs:
            title_tokens = self._tokens(d.title)
            text_tokens = Counter(self._tokens(d.text))
            score = 0.0
            for term in q:
                if term in text_tokens:
                    score += (1 + math.log(1 + text_tokens[term])) * self._idf(term)
                if term in title_tokens:
                    score += 2.5 * self._idf(term)
                else:  # partial/typo tolerance: prefix match
                    for t in set(text_tokens) | set(title_tokens):
                        if len(term) > 4 and (t.startswith(term[:4]) or term.startswith(t[:4])):
                            score += 0.4 * self._idf(term)
                            break
            if score > 0:
                scored.append(Document(**{**d.__dict__, "score": round(score, 3),
                                          "match_query": query}))
        scored.sort(key=lambda d: -d.score)
        return scored[:limit]

    # --------------------------------------------- format the agent's tool out
    def render_results(self, docs: list[Document], start_index: int = 0) -> str:
        """Emit the site's own tool_result text format (ref_id†title†url)."""
        blocks = []
        for i, d in enumerate(docs):
            ref = f"turn0search{start_index + i}"
            d.ref_id = ref
            blocks.append(f"[ref_id={ref}†{d.title}†{d.url}]\n"
                          f"Date: {d.date or 'unknown'}\n{d.snippet()}")
        return "\n\n".join(blocks)
