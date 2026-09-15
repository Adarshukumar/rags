"""End-to-end tests for the recovered workflow: typo decode → agent → chat system."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request

from zai_re.agent import Agent
from zai_re.chatstore import ChatStore
from zai_re.client import ZaiClient
from zai_re.corpus import Corpus
from zai_re.protocol import CompletionStream, Event, iter_sse_events
from zai_re.server_chat import serve
from zai_re.typo import decode

GARBLED = "deep search whats next elonmkusk goad of towdy"


class TestTypoDecoder(unittest.TestCase):
    def test_decodes_the_captured_garbled_prompt(self):
        d = decode(GARBLED)
        # exactly what the live model's thinking frame decoded by hand
        self.assertEqual(d.text, "deep search what's next elon musk goals of today")
        self.assertEqual(d.intent, "search")
        self.assertIn("elon musk", d.entities)
        kinds = {c.kind for c in d.corrections}
        self.assertIn("glue", kinds)        # elonmkusk -> elon musk
        self.assertIn("spelling", kinds)    # goad/towdy
        self.assertIn("contraction", kinds)  # whats -> what's

    def test_clean_prompt_is_untouched(self):
        d = decode("hey hi")
        self.assertEqual(d.text, "hey hi")
        self.assertFalse(d.changed)
        self.assertEqual(d.intent, "chat")

    def test_valid_inflections_are_not_corrected(self):
        d = decode("how many starships will fly in 2026?")
        self.assertIn("starships", d.text)
        self.assertEqual(d.intent, "question")

    def test_spacing_and_punctuation_preserved(self):
        d = decode("whats ur name ?")
        self.assertEqual(d.text, "what's your name ?")


class TestCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = Corpus.from_har()

    def test_documents_parsed_from_tool_responses(self):
        self.assertGreaterEqual(len(self.corpus.docs), 35)
        urls = {d.url for d in self.corpus.docs}
        self.assertGreaterEqual(len(urls), 35)      # de-duplicated by URL

    def test_search_finds_relevant_sources(self):
        hits = self.corpus.search("what's next elon musk goals of today", limit=5)
        self.assertTrue(hits)
        self.assertTrue(any("musk" in (h.title + h.text).lower() for h in hits))

    def test_render_uses_site_ref_format(self):
        hits = self.corpus.search("tesla robotaxi", limit=2)
        text = self.corpus.render_results(hits)
        self.assertRegex(text, r"\[ref_id=turn0search\d+†.+\]\nDate: ")


class TestAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = Agent(Corpus.from_har(), think_delay=0, chunk_delay=0)

    def _run(self, prompt, **kw):
        st = CompletionStream()
        for chunk in self.agent.run(prompt, **kw):
            for obj in iter_sse_events(chunk.decode()):
                st.feed(Event.from_wire(obj))
        return st, self.agent.last_result

    def test_search_turn_emits_full_phase_sequence(self):
        st, res = self._run(GARBLED)
        order = [p for p in ("thinking", "tool_call", "tool_response", "answer",
                            "other", "done") if st.phases.get(p)]
        self.assertEqual(order, ["thinking", "tool_call", "tool_response",
                                 "answer", "other", "done"])
        self.assertTrue(st.done)
        self.assertEqual(len(res.tool_calls), 2)          # search + open
        self.assertTrue(res.sources)
        self.assertIn("turn0search", st.answer_text)

    def test_chat_turn_has_no_tools(self):
        st, res = self._run("hey hi")
        self.assertNotIn("tool_call", st.phases)
        self.assertTrue(st.done)
        self.assertTrue(st.answer_text)

    def test_tool_arguments_reassemble_to_valid_json(self):
        _, res = self._run(GARBLED)
        call = res.tool_calls[0]
        args = json.loads(call["arguments"])
        self.assertIn("search_query", args)
        self.assertEqual(len(args["search_query"]), 3)

    def test_usage_frame_is_not_the_end(self):
        """usage must arrive before the final answer chunk, like the capture."""
        raw_frames = []
        for chunk in self.agent.run(GARBLED):
            for obj in iter_sse_events(chunk.decode()):
                raw_frames.append(obj["data"])
        phases = [f.get("phase") for f in raw_frames]
        usage_at = phases.index("other")
        done_at = phases.index("done")
        self.assertLess(usage_at, done_at)


class TestChatStore(unittest.TestCase):
    def test_linked_list_and_stubbed_assistant_nodes(self):
        store = ChatStore()
        rec = store.create(prompt="hey hi", model="x-preview-l")
        user = rec.path()[0]
        rec.add_assistant(user.id, "Hello!", reasoning="thinking…",
                          tool_calls=[{"name": "search"}], usage={"total_tokens": 5})
        wire = rec.wire()
        nodes = wire["chat"]["history"]["messages"]
        self.assertEqual(len(nodes), 2)
        assistant = [n for n in nodes.values() if n["role"] == "assistant"][0]
        self.assertNotIn("content", assistant)            # faithful to the live API
        self.assertIn("content", [n for n in nodes.values()
                                  if n["role"] == "user"][0])
        # debug escape hatch
        full = rec.wire(include_content=True)
        self.assertIn("content", [n for n in full["chat"]["history"]["messages"].values()
                                 if n["role"] == "assistant"][0])

    def test_background_title_and_tags(self):
        store = ChatStore(title_delay=0.05, tags_delay=0.05)
        rec = store.create(prompt=GARBLED, model="x-preview-l")
        store.schedule_background(rec, GARBLED)
        time.sleep(0.25)
        self.assertNotEqual(rec.title, "New Chat")
        self.assertTrue(rec.tags)
        tasks = {e["task"] for e in store.background_log}
        self.assertEqual(tasks, {"title_generation", "tags_generation"})


class TestServerEndToEnd(unittest.TestCase):
    """Boot the replica server and drive it with the recovered client."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = serve(port=0, host="127.0.0.1", think_delay=0, chunk_delay=0,
                          title_delay=0.05)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.15)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_full_flow_over_http(self):
        client = ZaiClient(f"http://127.0.0.1:{self.port}", timeout=30)
        ident = client.auths()
        self.assertEqual(ident.role, "local")
        models = client.fetch_models()
        self.assertTrue(models)

        chat = client.new_chat(GARBLED, model="x-preview-l")
        state = None
        for kind, payload in client.stream_turn(chat.id, GARBLED, model="x-preview-l",
                                                mcp_servers=["advanced-search"],
                                                auto_web_search=True):
            if kind == "result":
                state = payload
        self.assertIsNotNone(state)
        self.assertTrue(state.done)
        self.assertIn("tool_call", state.phases)
        self.assertIn("tool_response", state.phases)
        self.assertTrue(state.answer_text)

        time.sleep(0.3)
        tree = client.get_chat(chat.id)
        nodes = tree["chat"]["history"]["messages"]
        self.assertEqual(len(nodes), 2)                    # user + assistant stub
        assistant = [n for n in nodes.values() if n["role"] == "assistant"][0]
        self.assertNotIn("content", assistant)
        self.assertNotEqual(tree["title"], "New Chat")

    def test_health_reports_corpus(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health",
                                    timeout=10) as r:
            health = json.load(r)
        self.assertTrue(health["ok"])
        self.assertGreaterEqual(health["corpus_docs"], 35)


if __name__ == "__main__":
    unittest.main(verbosity=2)
