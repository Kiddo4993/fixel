import json
import stat

import httpx
import pytest

import fixel.brain as brain
from fixel.brain import ProviderError, Turn, make_provider
from fixel.brain.gemini import GeminiProvider
from fixel.brain.trial import TrialProvider, chunk_text, install_id, to_contents
from fixel.config import Config

URL = "https://trial.example"
ID = "0123456789abcdef0123456789abcdef"


def sse(*texts, thought=None):
    lines = []
    if thought:
        lines.append("data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": thought, "thought": True}]}}]}))
    for text in texts:
        lines.append("data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}))
    return "\n\n".join(lines) + "\n\n"


def provider_with(handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return TrialProvider(URL, ID, client=client), seen


async def collect(provider, **kw):
    return "".join([c async for c in provider.stream([Turn("user", "q", [b"PNG"])], "sys", **kw)])


async def test_streams_the_answer_and_reads_what_is_left():
    provider, seen = provider_with(lambda r: httpx.Response(
        200, text=sse("MODE: general\n", "---\nhi"), headers={"X-Fixel-Remaining": "2", "X-Fixel-Model": "gemini-3.6-flash"}
    ))
    assert await collect(provider) == "MODE: general\n---\nhi"
    assert provider.remaining == 2 and provider.last_model == "gemini-3.6-flash"
    sent = json.loads(seen[0].content)
    assert seen[0].url == f"{URL}/v1/ask"
    assert sent["install_id"] == ID and sent["system"] == "sys" and sent["deep"] is False
    assert sent["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/png"


async def test_thinking_is_not_shown_as_the_answer():
    provider, _ = provider_with(lambda r: httpx.Response(200, text=sse("answer", thought="secret reasoning")))
    assert await collect(provider) == "answer"


async def test_used_up_trial_says_how_to_add_a_key():
    provider, _ = provider_with(lambda r: httpx.Response(
        429, json={"error": "trial_used", "remaining": 0, "message": "You've used today's 3 free questions."}
    ))
    with pytest.raises(ProviderError) as raised:
        await collect(provider)
    assert raised.value.auth
    assert "3 free questions" in str(raised.value) and "/api gemini" in str(raised.value)


async def test_busy_trial_is_a_plain_error():
    provider, _ = provider_with(lambda r: httpx.Response(503, json={"error": "busy", "message": "Gemini is busy right now."}))
    with pytest.raises(ProviderError, match="busy") as raised:
        await collect(provider)
    assert not raised.value.auth


async def test_offline_is_a_friendly_error():
    def offline(request):
        raise httpx.ConnectError("no route")

    provider, _ = provider_with(offline)
    with pytest.raises(ProviderError, match="Can't reach Fixel's free trial"):
        await collect(provider)


def test_install_id_is_random_private_and_stable(tmp_path):
    first = install_id(tmp_path)
    assert len(first) == 32 and install_id(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "install-id").stat().st_mode) == 0o600
    (tmp_path / "install-id").write_text("../../etc")  # a damaged file gets replaced, not sent
    assert install_id(tmp_path) != "../../etc"


def test_contents_and_chunks():
    contents = to_contents([Turn("user", "q", [b"\x89PNG"]), Turn("assistant", "a")])
    assert [c["role"] for c in contents] == ["user", "model"]
    assert contents[1]["parts"] == [{"text": "a"}]
    assert chunk_text("data: not json") == "" and chunk_text(": keep-alive") == ""


def test_no_key_uses_the_trial_and_a_key_skips_it(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path, trial_url=URL)
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: None)
    assert isinstance(make_provider("gemini", cfg), TrialProvider)
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "own-key")
    assert isinstance(make_provider("gemini", cfg), GeminiProvider)  # your own key: never counted


def test_without_a_trial_server_you_need_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: None)
    with pytest.raises(ProviderError) as raised:
        make_provider("gemini", Config(home=tmp_path, trial_url=""))
    assert raised.value.auth


async def test_app_says_how_many_trial_questions_are_left(tmp_path, console):
    from fixel.app import FixelApp
    from fixel.brain.fake import FakeProvider
    from fixel.casefile import CaseFile
    from fixel.ui import UI

    provider = FakeProvider()
    provider.remaining = 1
    app = FixelApp(Config(home=tmp_path), UI(console), provider_factory=lambda a, c: provider,
                   casefile=CaseFile(tmp_path / "c.db"))
    app.select_model("gemini")
    await app.ask("what is 2+2")
    assert "1 question left today" in console.file.getvalue()


async def test_canvas_does_not_spend_trial_questions_on_plans(tmp_path, console):
    from datetime import datetime, timezone

    from fixel.brain.fake import FakeProvider
    from fixel.canvas.command import CanvasCommand
    from fixel.canvas.items import Assignment
    from fixel.canvas.store import CanvasStore
    from fixel.ui import UI

    provider = FakeProvider()
    provider.trial = True
    command = CanvasCommand(Config(home=tmp_path), UI(console), lambda: provider)
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    await command._plan(CanvasStore.load(tmp_path / "canvas.json"), [Assignment("assignment-1", "Lab", "CSA", now, "")], now)
    assert provider.calls == []
    assert "/api gemini" in console.file.getvalue()
