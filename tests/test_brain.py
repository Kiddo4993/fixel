import asyncio

import pytest

import fixel.brain as brain
from fixel.brain import ProviderError, Turn, make_provider
from fixel.brain.claude import ClaudeProvider, to_messages
from fixel.brain.fake import FakeProvider
from fixel.brain.gemini import GeminiProvider, to_contents
from fixel.config import Config


def test_to_messages_puts_images_first_and_one_cache_point():
    turns = [
        Turn("user", "what?", [b"img1", b"img2"]),
        Turn("assistant", "MODE: general\nKEY:\n---\nok"),
        Turn("user", "more"),
    ]
    messages = to_messages(turns)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    first = messages[0]["content"]
    assert [block["type"] for block in first] == ["image", "image", "text"]
    assert first[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "aW1nMQ=="}
    cached = [b for m in messages for b in m["content"] if "cache_control" in b]
    assert cached == [messages[-1]["content"][-1]]


def test_to_contents_maps_roles():
    contents = to_contents([Turn("user", "q", [b"i"]), Turn("assistant", "a")])
    assert [c.role for c in contents] == ["user", "model"]
    assert len(contents[0].parts) == 2
    assert contents[0].parts[1].text == "q"


async def test_fake_provider_streams_and_records():
    provider = FakeProvider(["MODE: math\nKEY: k\n---\nAnswer: 4"], chunk=3)
    out = "".join([c async for c in provider.stream([Turn("user", "2+2")], "sys")])
    assert out.endswith("Answer: 4")
    assert provider.calls[0][1] == "sys"


def test_make_provider_unknown_alias(tmp_path):
    with pytest.raises(ProviderError, match="Unknown model"):
        make_provider("nope", Config(home=tmp_path))


def test_make_provider_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: None)
    with pytest.raises(ProviderError) as excinfo:
        make_provider("sonnet", Config(home=tmp_path, free_only=False))
    assert excinfo.value.auth


def test_make_provider_picks_vendor(tmp_path, monkeypatch):
    from fixel.brain.openai import OpenAIProvider
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: f"key-{vendor}")
    cfg = Config(home=tmp_path, free_only=False)
    opus = make_provider("opus", cfg)
    assert isinstance(opus, ClaudeProvider) and opus.name == "claude-opus-5-5"
    assert isinstance(make_provider("gemini", cfg), GeminiProvider)
    gpt = make_provider("gpt", cfg)
    assert isinstance(gpt, OpenAIProvider) and gpt.name == "gpt-4o" 


def test_gemini_config_disables_automatic_function_calling():
    # Fixel uses no tools; leaving AFC on makes the SDK print a warning into Fixel's window on every question.
    from fixel.brain.gemini import make_config

    config = make_config("sys")
    assert config.system_instruction == "sys"
    assert config.automatic_function_calling.disable is True


# ---- faster answers: thinking levels and busy fallback

class _Chunk:
    def __init__(self, text):
        self.text = text


class FakeGeminiClient:
    """Stands in for genai.Client: per-model scripts of chunks, or an error code to raise."""

    def __init__(self, scripts):
        self.scripts = scripts  # model -> list of str chunks, or int error code, or (chunks, code)
        self.calls = []
        self.aio = self
        self.models = self

    async def generate_content_stream(self, *, model, contents, config):
        from google.genai import errors

        self.calls.append((model, config))
        script = self.scripts[model]

        def busy(code):
            return errors.ServerError(code, {"error": {"code": code, "message": "busy"}}) if code >= 500 else errors.ClientError(code, {"error": {"code": code, "message": "slow down"}})

        if isinstance(script, int):
            raise busy(script)

        if isinstance(script, dict):  # {"sleep": seconds, "text": [...]}: a slow model
            async def slow_chunks():
                await asyncio.sleep(script["sleep"])
                for text in script["text"]:
                    yield _Chunk(text)

            return slow_chunks()

        async def chunks():
            texts, code = script if isinstance(script, tuple) else (script, None)
            for text in texts:
                yield _Chunk(text)
            if code:
                raise busy(code)

        return chunks()


async def collect(provider, **kwargs):
    return "".join([c async for c in provider.stream([Turn("user", "q")], "sys", **kwargs)])


def test_gemini_thinking_levels():
    from google.genai import types

    from fixel.brain.gemini import make_config

    assert make_config("s", "gemini-3.6-flash").thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert make_config("s", "gemini-3.6-flash", deep=True).thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert make_config("s", "gemini-2.5-flash").thinking_config is None  # older models don't take levels


async def test_gemini_deep_flag_reaches_the_request():
    from google.genai import types

    client = FakeGeminiClient({"gemini-3.6-flash": ["ok"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", client=client)
    await collect(provider, deep=True)
    assert client.calls[0][1].thinking_config.thinking_level == types.ThinkingLevel.HIGH


async def test_gemini_falls_back_when_busy():
    client = FakeGeminiClient({"gemini-3.6-flash": 503, "gemini-3.5-flash": ["MODE: general\n", "---\nhi"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, retry_delay=0)
    assert await collect(provider) == "MODE: general\n---\nhi"
    assert [model for model, _ in client.calls] == ["gemini-3.6-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    assert provider.last_model == "gemini-3.5-flash"


async def test_gemini_rate_limit_also_falls_back():
    client = FakeGeminiClient({"gemini-3.6-flash": 429, "gemini-3.5-flash": ["ok"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, retry_delay=0)
    assert await collect(provider) == "ok"


async def test_gemini_all_busy_gives_friendly_error():
    client = FakeGeminiClient({"gemini-3.6-flash": 503, "gemini-3.5-flash": 503})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, retry_delay=0)
    with pytest.raises(ProviderError, match="busy"):
        await collect(provider)


async def test_gemini_never_switches_models_mid_answer():
    client = FakeGeminiClient({"gemini-3.6-flash": (["half an ans"], 503), "gemini-3.5-flash": ["other"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, retry_delay=0)
    with pytest.raises(ProviderError):
        await collect(provider)
    assert [model for model, _ in client.calls] == ["gemini-3.6-flash"]


async def test_gemini_answering_model_resets_each_question():
    client = FakeGeminiClient({"gemini-3.6-flash": ["ok"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client)
    await collect(provider)
    assert provider.last_model == "gemini-3.6-flash"


def test_claude_quick_vs_deep_thinking():
    provider = ClaudeProvider("k", "claude-sonnet-5-5", thinking=False)
    turns = [Turn("user", "q")]
    assert "thinking" not in provider.build_params(turns, "sys", deep=False)
    assert provider.build_params(turns, "sys", deep=True)["thinking"] == {"type": "adaptive"}
    assert ClaudeProvider("k", "m", thinking=True).build_params(turns, "sys", deep=False)["thinking"] == {"type": "adaptive"}
    assert provider.last_model == "claude-sonnet-5-5"


async def test_fake_provider_records_deep_and_answering_model():
    provider = FakeProvider(answered_by="backup")
    await collect(provider, deep=True)
    assert provider.calls[0][2] is True and provider.last_model == "backup"


def test_make_provider_passes_gemini_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "k")
    provider = make_provider("gemini", Config(home=tmp_path))
    assert provider.fallbacks == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]


async def test_gemini_moves_on_from_a_model_that_is_too_slow_to_start():
    client = FakeGeminiClient({"gemini-3.6-flash": {"sleep": 5, "text": ["late"]}, "gemini-3.5-flash": ["fast"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, slow_after=0.1)
    assert await collect(provider) == "fast"
    assert [model for model, _ in client.calls] == ["gemini-3.6-flash", "gemini-3.5-flash"]  # no retry of a slow model
    assert provider.last_model == "gemini-3.5-flash"


async def test_gemini_never_cuts_off_the_last_model():
    client = FakeGeminiClient({"gemini-3.6-flash": {"sleep": 0.3, "text": ["slow but here"]}})
    provider = GeminiProvider("k", "gemini-3.6-flash", client=client, slow_after=0.1)
    assert await collect(provider) == "slow but here"


async def test_gemini_deep_thinking_gets_more_patience():
    client = FakeGeminiClient({"gemini-3.6-flash": {"sleep": 0.2, "text": ["deep"]}, "gemini-3.5-flash": ["fast"]})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-3.5-flash"], client=client, slow_after=0.1)
    assert await collect(provider, deep=True) == "deep"


def test_make_provider_passes_slow_model_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "k")
    assert make_provider("gemini", Config(home=tmp_path)).slow_after == 20


# ---- free mode: never spend API credits

def test_free_only_refuses_paid_vendors(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "k")
    cfg = Config(home=tmp_path, free_only=True)
    for alias in ("sonnet", "opus", "gpt"):
        with pytest.raises(ProviderError, match="no free tier"):
            make_provider(alias, cfg)
    assert isinstance(make_provider("gemini", cfg), GeminiProvider)


def test_free_only_refuses_gemini_models_without_a_free_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "k")
    cfg = Config(home=tmp_path, free_only=True, models={"gemini": "gemini-3.1-pro-preview"})
    with pytest.raises(ProviderError, match="isn't on Gemini's free tier"):
        make_provider("gemini", cfg)


def test_free_only_drops_paid_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "get_api_key", lambda vendor: "k")
    cfg = Config(home=tmp_path, free_only=True, gemini_fallbacks=["gemini-3.5-flash", "gemini-2.5-flash"])
    assert make_provider("gemini", cfg).fallbacks == ["gemini-2.5-flash"]
    cfg.free_only = False
    assert make_provider("gemini", cfg).fallbacks == ["gemini-3.5-flash", "gemini-2.5-flash"]


def test_free_only_reads_from_config_file(tmp_path):
    from fixel.config import load_config

    (tmp_path / "config.toml").write_text("free_only = false\n")
    assert load_config(tmp_path).free_only is False
    assert load_config(tmp_path / "missing").free_only is True  # free unless you opt out


def test_a_config_typo_is_a_friendly_error(tmp_path):
    from fixel.config import ConfigError, load_config

    (tmp_path / "config.toml").write_text("default_model = gemini\n")  # missing quotes
    with pytest.raises(ConfigError, match="has a typo"):
        load_config(tmp_path)


def _quota_error(daily: bool, delay: str = "37s"):
    from google.genai import errors

    quota_id = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return errors.ClientError(429, {"error": {"code": 429, "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
    ]}})


class QuotaClient(FakeGeminiClient):
    """Models listed in `errors` raise that exception; the rest follow their script."""

    def __init__(self, scripts, errors):
        super().__init__(scripts)
        self.errors = errors

    async def generate_content_stream(self, *, model, contents, config):
        if model in self.errors:
            self.calls.append((model, config))
            raise self.errors[model]
        return await super().generate_content_stream(model=model, contents=contents, config=config)


async def test_gemini_rate_limit_moves_on_without_retrying_the_same_model():
    client = QuotaClient({"gemini-2.5-flash": ["ok"]}, {"gemini-3.6-flash": _quota_error(daily=False)})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-2.5-flash"], client=client, retry_delay=0)
    assert await collect(provider) == "ok"
    assert [model for model, _ in client.calls] == ["gemini-3.6-flash", "gemini-2.5-flash"]


async def test_gemini_skips_a_model_whose_daily_quota_ran_out():
    client = QuotaClient({"gemini-2.5-flash": ["ok"]}, {"gemini-3.6-flash": _quota_error(daily=True)})
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-2.5-flash"], client=client, retry_delay=0)
    await collect(provider)
    client.calls.clear()
    assert await collect(provider) == "ok"
    assert [model for model, _ in client.calls] == ["gemini-2.5-flash"]  # no request wasted on the empty one


async def test_gemini_says_how_long_to_wait_on_the_free_tier():
    client = QuotaClient({}, {"gemini-3.6-flash": _quota_error(daily=False, delay="12.4s")})
    provider = GeminiProvider("k", "gemini-3.6-flash", client=client, retry_delay=0)
    with pytest.raises(ProviderError, match="Wait about 12s"):
        await collect(provider)


async def test_gemini_says_when_every_free_model_is_out_for_the_day():
    errs = {"gemini-3.6-flash": _quota_error(daily=True), "gemini-2.5-flash": _quota_error(daily=True)}
    provider = GeminiProvider("k", "gemini-3.6-flash", fallbacks=["gemini-2.5-flash"], client=QuotaClient({}, errs), retry_delay=0)
    for _ in range(2):  # the second time, without sending anything
        with pytest.raises(ProviderError, match="daily limit"):
            await collect(provider)


async def test_gemini_skips_a_retired_fallback():
    client = FakeGeminiClient({"gemini-3.6-flash": 503, "gemini-old": 404, "gemini-2.5-flash": ["ok"]})
    provider = GeminiProvider(
        "k", "gemini-3.6-flash", fallbacks=["gemini-old", "gemini-2.5-flash"], client=client, retry_delay=0
    )
    assert await collect(provider) == "ok"


async def test_gemini_missing_last_model_still_says_not_found():
    provider = GeminiProvider("k", "gemini-nope", client=FakeGeminiClient({"gemini-nope": 404}), retry_delay=0)
    with pytest.raises(ProviderError, match="wasn't found"):
        await collect(provider)
