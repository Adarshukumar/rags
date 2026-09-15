"""zai_re.protocol — wire format of chat.z.ai's streaming agent protocol.

Recovered from a HAR capture of https://chat.z.ai (2026-09-15) — see
docs/zai-protocol-spec.md. Everything here is pure stdlib so the protocol can
be exercised without any third-party dependency.

Wire summary
------------
POST /api/v2/chat/completions           -> text/event-stream
  data: {"type":"chat:completion","data":{...}}\\n\\n     (repeated)

The inner `data` object is phase-tagged. Phases observed:

  thinking      delta_content   reasoning stream (private chain of thought)
  tool_call     delta_name + delta_arguments (arg JSON arrives CHUNKED)
  tool_response tool_name, status, delta_content  (the tool's output)
  answer        delta_content   the visible answer
  other         usage           token accounting for that hop
  done          done: true      terminal frame

Note: `usage` can arrive *between* answer deltas — a client that stops at the
first `usage` frame truncates the answer (observed in the capture: the final
"." after the usage frame). Only `phase == "done"` ends the stream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

SSE_DATA_PREFIX = "data:"
EVENT_TYPE = "chat:completion"

PHASE_THINKING = "thinking"
PHASE_ANSWER = "answer"
PHASE_TOOL_CALL = "tool_call"
PHASE_TOOL_RESPONSE = "tool_response"
PHASE_OTHER = "other"
PHASE_DONE = "done"


# --------------------------------------------------------------------------- SSE
def iter_sse_events(raw: str) -> Iterator[dict]:
    """Yield the JSON payload of every `data:` line in an SSE body.

    Tolerates: missing blank-line separators, `data:` without a space, and
    non-JSON keep-alive lines (skipped).
    """
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith(SSE_DATA_PREFIX):
            continue
        payload = line[len(SSE_DATA_PREFIX):].lstrip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def iter_sse_lines(chunks: Iterable[str]) -> Iterator[dict]:
    """Incremental variant: feed decoded byte-chunks, get events as they land.

    Handles events split across network chunks (the capture shows frames up to
    ~14 KB being delivered across many TCP reads).
    """
    buf = ""
    for chunk in chunks:
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith(SSE_DATA_PREFIX):
                continue
            payload = line[len(SSE_DATA_PREFIX):].lstrip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
    return


# --------------------------------------------------------------------------- frames
@dataclass
class Event:
    """One decoded `chat:completion` frame."""

    type: str
    phase: Optional[str] = None
    delta_content: Optional[str] = None
    delta_name: Optional[str] = None
    delta_arguments: Optional[str] = None
    tool_name: Optional[str] = None
    status: Optional[str] = None
    usage: Optional[dict] = None
    done: bool = False
    metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, obj: dict) -> "Event":
        data = obj.get("data") or {}
        return cls(
            type=obj.get("type", ""),
            phase=data.get("phase"),
            delta_content=data.get("delta_content"),
            delta_name=data.get("delta_name"),
            delta_arguments=data.get("delta_arguments"),
            tool_name=data.get("tool_name"),
            status=data.get("status"),
            usage=data.get("usage"),
            done=bool(data.get("done")),
            metadata=data.get("metadata") or {},
            raw=obj,
        )


@dataclass
class ToolCall:
    """A tool call reassembled from streamed argument fragments."""

    name: str
    call_id: Optional[str]
    arguments_raw: str = ""

    @property
    def arguments(self) -> Any:
        try:
            return json.loads(self.arguments_raw or "{}")
        except json.JSONDecodeError:
            return None


class CompletionStream:
    """State machine that folds a stream of Events into a turn result.

    This is the core of the reverse engineering: it shows exactly what the
    browser does with the frame sequence.
    """

    def __init__(self) -> None:
        self.thinking: list[str] = []
        self.answer: list[str] = []
        self.tool_calls: list[ToolCall] = []
        self.tool_responses: list[dict] = []
        self.usage: list[dict] = []
        self.phases: dict[str, int] = {}
        self.frames = 0
        self.done = False
        self._open_call: Optional[ToolCall] = None

    # -- ingest -------------------------------------------------------------
    def feed(self, ev: Event) -> Optional[str]:
        """Apply one frame. Returns an optional incremental text delta for the
        caller to render (thinking and answer both stream)."""
        if ev.type != EVENT_TYPE:
            return None
        self.frames += 1
        phase = ev.phase or ""
        self.phases[phase] = self.phases.get(phase, 0) + 1

        if phase == PHASE_THINKING and ev.delta_content is not None:
            self.thinking.append(ev.delta_content)
            return ev.delta_content

        if phase == PHASE_ANSWER and ev.delta_content is not None:
            self.answer.append(ev.delta_content)
            return ev.delta_content

        if phase == PHASE_TOOL_CALL:
            if ev.delta_name:
                self._open_call = ToolCall(
                    name=ev.delta_name,
                    call_id=ev.metadata.get("tool_call_id"),
                )
                self.tool_calls.append(self._open_call)
            if ev.delta_arguments and self._open_call is not None:
                # arguments arrive as fragments; the first fragment does NOT
                # carry the tool_call_id (metadata switches to {"type": "function"})
                self._open_call.arguments_raw += ev.delta_arguments
            return None

        if phase == PHASE_TOOL_RESPONSE:
            self.tool_responses.append({
                "tool_name": ev.tool_name,
                "status": ev.status,
                "content": ev.delta_content or "",
                "metadata": ev.metadata,
            })
            return None

        if phase == PHASE_OTHER and ev.usage:
            self.usage.append(ev.usage)
            return None

        if ev.done:
            self.done = True
        return None

    # -- results ------------------------------------------------------------
    @property
    def thinking_text(self) -> str:
        return "".join(self.thinking)

    @property
    def answer_text(self) -> str:
        return "".join(self.answer)

    def feed_events(self, events: Iterable[dict]) -> Iterator[str]:
        for obj in events:
            delta = self.feed(Event.from_wire(obj))
            if delta:
                yield delta
