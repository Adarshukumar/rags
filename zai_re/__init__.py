"""zai_re — reverse-engineered client + replay harness for chat.z.ai.

Modules
    protocol      wire format: SSE frames, phases, tool-call reassembly
    client        stdlib client: auth -> chat -> streaming completion
    har_source    reads the captured HAR as ground truth (credentials scrubbed)
    mock_server   HTTP replay server speaking the same protocol
    verify        parity harness: mock + client vs the captured bytes

Quick start
    python3 -m zai_re.mock_server --port 8801 --speed 8     # replay server + demo UI
    python3 -m zai_re.verify                                # parity report
    python3 -m unittest discover -s tests -t .              # unit tests

Scope: this is protocol analysis of a capture made by the repo owner. The
captcha attestation the live site requires is passed through, never bypassed.
"""

__all__ = ["protocol", "client", "har_source", "mock_server"]
__version__ = "0.1.0"
