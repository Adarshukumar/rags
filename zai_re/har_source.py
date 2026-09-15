"""zai_re.har_source — turn the captured HAR into reusable ground truth.

The HAR is the *specimen*: this module extracts

  * every captured completion stream (prompt -> raw SSE bytes)
  * the JSON payloads of the metadata endpoints
  * the identity, with credentials scrubbed

so both the mock server (replay) and the verifier (parity check) work from the
same source of truth. Read-only: the HAR is never modified.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

DEFAULT_HAR = os.environ.get(
    "ZAI_HAR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "chat.z.ai.har"),
)


def _decode(content: dict) -> str:
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64":
        return base64.b64decode(text).decode("utf-8", "replace")
    return text


@dataclass
class CapturedStream:
    entry_index: int
    chat_id: str
    model: str
    prompt: str
    raw_sse: str
    request_query: dict = field(default_factory=dict)
    request_body: dict = field(default_factory=dict)

    @property
    def frames(self) -> int:
        return sum(1 for line in self.raw_sse.splitlines() if line.startswith("data:"))


class HarSource:
    def __init__(self, path: str = DEFAULT_HAR) -> None:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            self.log = json.load(fh)["log"]
        self.entries = self.log["entries"]

    # ------------------------------------------------------------- payloads
    def entry(self, index: int) -> dict:
        return self.entries[index]

    def response_json(self, index: int):
        return json.loads(_decode(self.entries[index]["response"]["content"]))

    def _find(self, method: str, path_fragment: str) -> Optional[int]:
        for i, e in enumerate(self.entries):
            u = e["request"]["url"]
            if e["request"]["method"] == method and path_fragment in u:
                return i
        return None

    def config_payload(self) -> dict:
        return self.response_json(self._find("GET", "/api/config"))

    def models_payload(self) -> dict:
        return self.response_json(self._find("GET", "/api/models"))

    def identity_payload(self) -> dict:
        data = self.response_json(self._find("GET", "/api/v1/auths/"))
        # scrub the real bearer token / ids before any of this is written to disk
        data["token"] = "<scrubbed-captured-token>"
        data["id"] = "<scrubbed-user-id>"
        data["email"] = "guest@guest.com"
        data["name"] = "Guest"
        return data

    def chat_new_request(self) -> dict:
        i = self._find("POST", "/api/v1/chats/new")
        return json.loads(self.entries[i]["request"]["postData"]["text"])

    def chat_get_payload(self) -> dict:
        i = self._find("GET", "/api/v1/chats/")
        return self.response_json(i)

    # -------------------------------------------------------------- streams
    def streams(self) -> list[CapturedStream]:
        out: list[CapturedStream] = []
        for i, e in enumerate(self.entries):
            if "/api/v2/chat/completions" not in e["request"]["url"]:
                continue
            body = json.loads(e["request"]["postData"]["text"])
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(e["request"]["url"]).query)
            out.append(CapturedStream(
                entry_index=i,
                chat_id=body.get("chat_id", ""),
                model=body.get("model", ""),
                prompt=body.get("signature_prompt", ""),
                raw_sse=_decode(e["response"]["content"]),
                request_query={k: v[0] for k, v in q.items()},
                request_body=body,
            ))
        return out

    def stream_for_prompt(self, prompt: str) -> Optional[CapturedStream]:
        for s in self.streams():
            if s.prompt.strip().lower() == prompt.strip().lower():
                return s
        return None

    def iter_completion_urls(self) -> Iterator[str]:
        for e in self.entries:
            if "/api/v2/chat/completions" in e["request"]["url"]:
                yield e["request"]["url"]
