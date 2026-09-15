"""Unit tests for the recovered chat.z.ai protocol layer (stdlib unittest)."""

from __future__ import annotations

import json
import unittest

from zai_re.har_source import HarSource
from zai_re.protocol import (CompletionStream, Event, iter_sse_events,
                             iter_sse_lines)


def frame(**data) -> str:
    return "data: " + json.dumps({"type": "chat:completion", "data": data}) + "\n\n"


class TestSSEParsing(unittest.TestCase):
    def test_basic_frames(self):
        raw = frame(delta_content="a", phase="answer") + frame(done=True, phase="done")
        events = list(iter_sse_events(raw))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["data"]["delta_content"], "a")
        self.assertTrue(events[1]["data"]["done"])

    def test_noise_and_keepalives_ignored(self):
        raw = ("event: ping\n: comment\n" + frame(delta_content="x", phase="answer")
               + "data: not-json\n\ndata: [DONE]\n\n")
        events = list(iter_sse_events(raw))
        self.assertEqual(len(events), 1)

    def test_incremental_reader_handles_split_frames(self):
        raw = frame(delta_content="hel", phase="answer") + frame(
            delta_content="lo", phase="answer") + frame(done=True, phase="done")
        # feed 7 bytes at a time -> every frame is split mid-JSON
        chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
        events = [Event.from_wire(o) for o in iter_sse_lines(chunks)]
        st = CompletionStream()
        for ev in events:
            st.feed(ev)
        self.assertEqual(st.answer_text, "hello")
        self.assertTrue(st.done)


class TestCompletionStream(unittest.TestCase):
    def test_thinking_and_answer_are_separated(self):
        st = CompletionStream()
        for ev in [frame(delta_content="think", phase="thinking"),
                   frame(delta_content="done.", phase="thinking"),
                   frame(delta_content="Hi", phase="answer"),
                   frame(delta_content=" there", phase="answer"),
                   frame(done=True, phase="done")]:
            st.feed(Event.from_wire(json.loads(ev[6:].strip())))
        self.assertEqual(st.thinking_text, "thinkdone.")
        self.assertEqual(st.answer_text, "Hi there")
        self.assertEqual(st.phases, {"thinking": 2, "answer": 2, "done": 1})

    def test_usage_frame_mid_answer_does_not_truncate(self):
        """The trap seen in the capture: usage lands BEFORE the final delta."""
        st = CompletionStream()
        seq = [
            {"delta_content": "Hello", "phase": "answer"},
            {"delta_content": " world", "phase": "answer"},
            {"phase": "other", "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                         "total_tokens": 3, "prompt_tokens_details": {}}},
            {"delta_content": ".", "phase": "answer"},
            {"phase": "done", "done": True},
        ]
        for d in seq:
            st.feed(Event.from_wire({"type": "chat:completion", "data": d}))
        self.assertEqual(st.answer_text, "Hello world.")
        self.assertEqual(len(st.usage), 1)
        self.assertTrue(st.done)

    def test_tool_call_arguments_are_reassembled(self):
        """Arguments stream as fragments; only the first carries the call id."""
        st = CompletionStream()
        seq = [
            {"phase": "tool_call", "delta_name": "search",
             "delta_arguments": '{"search_',
             "metadata": {"tool_call_id": "call_1", "type": "function"}},
            {"phase": "tool_call", "delta_arguments": 'query":[{"q": "x", ',
             "metadata": {"type": "function"}},
            {"phase": "tool_call", "delta_arguments": '"recency": 7}]}',
             "metadata": {"type": "function"}},
        ]
        for d in seq:
            st.feed(Event.from_wire({"type": "chat:completion", "data": d}))
        self.assertEqual(len(st.tool_calls), 1)
        call = st.tool_calls[0]
        self.assertEqual(call.name, "search")
        self.assertEqual(call.call_id, "call_1")
        self.assertEqual(call.arguments, {"search_query": [{"q": "x", "recency": 7}]})

    def test_tool_response_captured(self):
        st = CompletionStream()
        st.feed(Event.from_wire({"type": "chat:completion", "data": {
            "phase": "tool_response", "tool_name": "open", "status": "completed",
            "delta_content": "Ref id turn0search12 is invalid",
            "metadata": {"tool_call_id": "call_9"}}}))
        self.assertEqual(len(st.tool_responses), 1)
        self.assertEqual(st.tool_responses[0]["tool_name"], "open")
        self.assertIn("invalid", st.tool_responses[0]["content"])


class TestHarGroundTruth(unittest.TestCase):
    """These run against the real capture; skipped if it is absent."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.src = HarSource()
        except FileNotFoundError:
            raise unittest.SkipTest("chat.z.ai.har not present")

    def test_three_streams_with_known_prompts(self):
        prompts = [s.prompt for s in self.src.streams()]
        self.assertEqual(prompts, ["hey hi", "whats ur name ?",
                                   "deep search whats next elonmkusk goad of towdy"])

    def test_deep_search_stream_shape(self):
        s = [x for x in self.src.streams() if x.prompt.startswith("deep search")][0]
        st = CompletionStream()
        for obj in iter_sse_events(s.raw_sse):
            st.feed(Event.from_wire(obj))
        self.assertEqual(st.frames, 504)
        self.assertEqual(len(st.tool_calls), 4)          # 3 x search + 1 x open
        self.assertEqual([c.name for c in st.tool_calls],
                         ["search", "search", "search", "open"])
        self.assertEqual(len(st.tool_responses), 4)
        self.assertEqual(len(st.usage), 4)
        self.assertTrue(st.done)
        self.assertIn("trillionaire", st.answer_text.lower())

    def test_identity_payload_is_scrubbed(self):
        ident = self.src.identity_payload()
        self.assertEqual(ident["token"], "<scrubbed-captured-token>")
        self.assertNotIn("eyJ", json.dumps(ident))


if __name__ == "__main__":
    unittest.main(verbosity=2)
