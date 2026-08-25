"""Reasoning-effort dialects, system presets, and wire translation.

Each OpenAI-compatible provider speaks ``reasoning_effort`` with a DIFFERENT raw
enum. The presets below are the system defaults; a model entry may override them
with its own raw value list (``reasoning_efforts``). Values pass through VERBATIM
— no label translation — with
two compat rules: DeepSeek maps ``medium``/``xhigh`` → ``high``, and the
sentinels ``none`` / ``off`` disable thinking.
"""

from typing import Any

# dialect -> (ordered raw values, default value)
PRESETS: dict[str, tuple[list[str], str]] = {
    "openai": (["minimal", "low", "medium", "high"], "medium"),
    "deepseek": (["low", "high", "max"], "high"),
    "glm": (["max", "xhigh", "high", "medium", "low", "minimal", "none"], "max"),
    "qwen": (["low", "medium", "xhigh"], "xhigh"),
}

# Values that disable thinking (send thinking:{type:disabled} on thinking
# dialects; omit reasoning_effort on plain OpenAI-compatible endpoints).
_DISABLED = ("none", "off")

# The "provider default" sentinel: selecting it sends NO reasoning fields at all,
# deferring to the server. It is a runtime choice, not a real wire value.
DEFAULT_EFFORT = "default"

# Dialects whose non-disabled levels ride a `thinking: {type: enabled}` wrapper.
_THINKING_DIALECTS = ("deepseek", "glm", "qwen")


def detect_dialect(model_name: str = "", base_url: str | None = None, provider: str = "") -> str:
    """Classify an entry's endpoint dialect.

    The model NAME is the primary signal (e.g. ``glm-4.6``, ``qwen3``,
    ``deepseek-chat``) because the reasoning-effort enum varies per model, not
    per vendor; base_url and the provider label are fallbacks for empty/alias
    model names.
    """
    mn = model_name.lower()
    if "deepseek" in mn:
        return "deepseek"
    if "glm" in mn or "zhipu" in mn or "bigmodel" in mn:
        return "glm"
    if "qwen" in mn or "dashscope" in mn or "alibaba" in mn:
        return "qwen"
    key = f"{base_url or ''} {provider}".lower()
    if "deepseek" in key:
        return "deepseek"
    if "bigmodel" in key or "zhipu" in key or "glm" in key:
        return "glm"
    if "dashscope" in key or "qwen" in key or "alibaba" in key:
        return "qwen"
    return "openai"


def _dialect(entry: dict[str, Any]) -> str:
    return detect_dialect(
        entry.get("model_name", ""), entry.get("base_url"), entry.get("provider", "")
    )


def values_for(entry: dict[str, Any]) -> list[str]:
    """The raw reasoning values for an entry (override replaces the preset)."""
    override = entry.get("reasoning_efforts")
    if isinstance(override, list) and override:
        return [str(v) for v in override if str(v).strip()]
    return list(PRESETS[_dialect(entry)][0])


def default_for(entry: dict[str, Any]) -> str | None:
    """The entry's preselected reasoning value, or None when it has no values.

    The universal default is ``DEFAULT_EFFORT`` ("default" — send no reasoning
    fields, defer to the server). We deliberately do NOT preselect the
    per-dialect preset default: the enum varies per model and the
    parameter-free server default is the safer choice.
    """
    values = values_for(entry)
    if not values:
        return None
    return DEFAULT_EFFORT


def options_for(entry: dict[str, Any]) -> list[str]:
    """The full selectable list: the 'default' sentinel first, then raw values."""
    return [DEFAULT_EFFORT] + values_for(entry)


def coerce_effort(value: str | None) -> str | None:
    """Normalize a user-facing effort to the provider value (None = don't send)."""
    if value is None or value == "" or value == DEFAULT_EFFORT:
        return None
    return value


def wire_for(dialect: str, value: str) -> dict[str, Any]:
    """The request-body fields for a selected reasoning value (empty = none)."""
    if value in _DISABLED:
        if dialect in _THINKING_DIALECTS:
            return {"thinking": {"type": "disabled"}}
        return {}
    if dialect == "deepseek":
        if value in ("medium", "xhigh"):
            value = "high"
        return {"thinking": {"type": "enabled"}, "reasoning_effort": value}
    if dialect in _THINKING_DIALECTS:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": value}
    return {"reasoning_effort": value}
