"""Model providers. `make_provider` turns a config alias into a ready provider."""

from __future__ import annotations

from ..config import FREE_GEMINI_MODELS, PRICING_URL, Config, get_api_key, provider_for_model
from .base import Provider, ProviderError, Turn

__all__ = ["Provider", "ProviderError", "Turn", "make_provider"]


def make_provider(alias: str, cfg: Config) -> Provider:
    if alias not in cfg.models:
        raise ProviderError(f"Unknown model '{alias}'. Choose one of: {', '.join(cfg.models)}.")
    model = cfg.models[alias]
    vendor = provider_for_model(model)
    if cfg.free_only:
        refusal = free_tier_refusal(model, vendor)
        if refusal:
            raise ProviderError(refusal)
    key = get_api_key(vendor)
    if not key and vendor == "gemini" and cfg.trial_url:  # no key yet: the free trial answers instead
        from .trial import TrialProvider, install_id

        return TrialProvider(cfg.trial_url, install_id(cfg.home))
    if not key:
        labels = {"gemini": "Gemini", "anthropic": "Anthropic", "openai": "OpenAI"}
        label = labels.get(vendor, vendor.title())
        raise ProviderError(f"No {label} API key yet. Run `/api {vendor}` or `fixel setup`.", auth=True)
    if vendor == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(
            key, model, fallbacks=gemini_fallbacks(model, cfg), slow_after=cfg.slow_model_seconds
        )
    if vendor == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(key, model)
    from .claude import ClaudeProvider

    return ClaudeProvider(key, model, max_tokens=cfg.max_tokens, thinking=cfg.thinking)


def free_tier_refusal(model: str, vendor: str) -> str | None:
    """Why `free_only` won't use this model, or None if it's on Gemini's free tier."""
    if vendor != "gemini":
        name = {"anthropic": "Claude", "openai": "OpenAI"}.get(vendor, vendor.title())
        return (
            f"{name} has no free tier, so it would spend API credits. Fixel is in free mode: use /model gemini, "
            "or set free_only = false in ~/.fixel/config.toml to allow paid models."
        )
    if model not in FREE_GEMINI_MODELS:
        return (
            f"'{model}' isn't on Gemini's free tier ({PRICING_URL}). Pick a free model in [models] of "
            "~/.fixel/config.toml (e.g. gemini-3.6-flash), or set free_only = false."
        )
    return None


def gemini_fallbacks(model: str, cfg: Config) -> list[str]:
    """Models to try when `model` is busy. In free mode only free-tier ones: a paid-only model just
    answers 'rate limited' on a free key, and costs money on a paid one."""
    return [m for m in cfg.gemini_fallbacks if m != model and (not cfg.free_only or m in FREE_GEMINI_MODELS)]
