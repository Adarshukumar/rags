"""zai_re — reverse-engineered client + replay harness for chat.z.ai.

Modules
    protocol      wire format: SSE frames, phases, tool-call reassembly
    client        stdlib client: auth -> chat -> streaming completion
    har_source    reads the captured HAR as ground truth (credentials scrubbed)
    mock_server   HTTP replay server speaking the same protocol
    verify        parity harness: mock + client vs the captured bytes

    typo          typo-tolerant intent decoding (repairs garbled prompts)
    corpus        the captured tool_response documents + a search tool over them
    chatstore     server-side chat system: linked-list tree, title/tags workers
    agent         the turn engine: thinking -> tools -> answer -> usage -> done
    server_chat   working chatbot (routes mirror the real site) + web UI
    live          run the recovered protocol against the real chat.z.ai
    workflow_demo narrated end-to-end run of the whole flow

Quick start
    python3 -m zai_re.mock_server --port 8801 --speed 8     # replay server + demo UI
    python3 -m zai_re.server_chat --port 8802               # working chatbot (replica)
    python3 -m zai_re.workflow_demo                         # narrated end-to-end flow
    python3 -m zai_re.verify                                # parity report
    python3 -m unittest discover -s tests -t .              # 25 tests
    python3 -m zai_re.live --url '<captured URL>' --prompt 'hey hi'   # real model
        (only where chat.z.ai is reachable — this dataset cannot reach it)

Scope: this is protocol analysis of a capture made by the repo owner. The
captcha attestation the live site requires is passed through, never bypassed.
"""

__all__ = ["protocol", "client", "har_source", "mock_server"]
__version__ = "0.1.0"
