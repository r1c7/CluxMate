"""Build an LLMProvider from a config model entry.

An entry has {api_type, provider, base_url, api_key, model_name, context_1m}.
`api_type` is stored for future API families; every entry currently builds an
OpenAIProvider — OpenAI's API also covers every OpenAI-compatible endpoint
(DeepSeek and Qwen, which are just a base_url).

max_tokens defaults to 32768 for every entry: thinking models (Qwen3,
R1-style) charge reasoning tokens against the SAME output budget, and a long
reasoning phase can easily exhaust a smaller cap before emitting a single
reply token — the completion then gets cut off mid-thought and the turn ends
with no visible answer.
"""

from typing import Any

from .base import LLMProvider


def _default_max_tokens() -> int:
    return 32768


def build_provider(entry: dict[str, Any]) -> LLMProvider:
    api_key = entry.get("api_key", "") or None
    base_url = entry.get("base_url", "") or None
    model = entry.get("model_name", "")
    # Per-model override (0/absent → the 32768 default).
    override = entry.get("max_tokens") or 0

    from .openai import OpenAIProvider
    return OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=override or _default_max_tokens(),
        provider=entry.get("provider", ""),
    )
