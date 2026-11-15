"""The free trial: a few Gemini answers a day through Fixel's trial server, for people with no key yet.

The server (server/trial) holds the Gemini key and does the counting; this side only sends the
conversation and a random install id, and reads back Gemini's streamed answer.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import ProviderError, Turn

INSTALL_FILE = "install-id"
ADD_KEY = "Add your own Gemini key with /api gemini. It's free at aistudio.google.com/apikey."


def install_id(home: Path) -> str:
    """A random id for this Mac's Fixel, so the trial server can count its questions. Nothing personal in it."""
    path = home / INSTALL_FILE
    try:
        saved = path.read_text().strip()
    except OSError:
        saved = ""
    if len(saved) == 32 and all(c in "0123456789abcdef" for c in saved):
        return saved
    new = uuid.uuid4().hex
    home.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(new)
    return new


def to_contents(turns: list[Turn]) -> list[dict]:
    """Turns → Gemini's REST `contents` (the trial server passes them straight to Google)."""
    return [
        {
            "role": "user" if turn.role == "user" else "model",
            "parts": [
                *({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}} for img in turn.images),
                {"text": turn.text},
            ],
        }
        for turn in turns
    ]


def chunk_text(line: str) -> str:
    """The answer text in one `data: {...}` line of Gemini's stream (thinking parts left out)."""
    if not line.startswith("data:"):
        return ""
    try:
        data = json.loads(line[5:].strip())
    except ValueError:
        return ""
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))


class TrialProvider:
    trial = True  # /canvas checks this: planning would spend the day's few questions

    def __init__(self, url: str, install: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/") + "/v1/ask"
        self.install = install
        self.name = "free trial (Gemini)"
        self.last_model = self.name
        self.remaining: int | None = None  # free questions left today, once the server has said
        self._client = client

    async def stream(self, turns: list[Turn], system: str, *, deep: bool = False) -> AsyncIterator[str]:
        body = {"install_id": self.install, "system": system, "contents": to_contents(turns), "deep": deep}
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0))
        try:
            async with client.stream("POST", self.url, json=body) as response:
                self._read_headers(response)
                if response.status_code != 200:
                    raise self._error(response.status_code, await response.aread())
                async for line in response.aiter_lines():
                    text = chunk_text(line)
                    if text:
                        yield text
        except httpx.TransportError as e:
            raise ProviderError("Can't reach Fixel's free trial. Check your internet, then press Enter.") from e
        finally:
            if self._client is None:
                await client.aclose()

    def _read_headers(self, response: httpx.Response) -> None:
        model = response.headers.get("X-Fixel-Model")
        if model:
            self.last_model = model
        left = response.headers.get("X-Fixel-Remaining", "")
        if left.isdigit():
            self.remaining = int(left)

    def _error(self, status: int, raw: bytes) -> ProviderError:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        if status == 429 and isinstance(payload, dict) and payload.get("error") == "trial_used":
            return ProviderError(f"{message or 'The free trial is used up for today.'} {ADD_KEY}", auth=True)
        return ProviderError(message or f"Fixel's free trial answered {status}. Try again in a minute.")
