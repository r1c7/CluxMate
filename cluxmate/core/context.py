"""Context window management — estimate size and compact when over budget.

The agent loop accumulates messages across turns and tool round-trips with no
bound. compact() shrinks an over-budget message list while preserving API
validity: an assistant message carrying tool_calls must keep its paired
tool_results, and the kept recent tail must not begin with an orphaned
tool_result (both Anthropic and OpenAI 400 otherwise).

Compaction is a single region-replace: the middle between the preserved head
(system + first user message) and the recent tail is collapsed into one summary
message. The caller records that replacement as a session-log surface op so the
compacted transcript stays replayable (see ``AgentLoop._log_compaction``). A
failed summarize falls back to a truncation note so a turn never hard-fails.

Token counts are char/4 estimates (no tokenizer dependency); the agent loop
calibrates against the provider's real usage where available.
"""

import json
from typing import Any

CHARS_PER_TOKEN = 4

# ``user/message`` sources that are environment injections (memory/skills/mode/
# compaction/interruption/hook), not human turns. ``_split_head`` skips them when
# finding the original-task anchor so the head starts at the first HUMAN message.
ENV_SOURCES = frozenset({"memory", "skill", "mode", "compaction", "interruption", "hook"})

SUMMARY_PROMPT = (
    "You are compressing an earlier portion of a coding-agent conversation to "
    "save context. Summarize the messages below into a concise but complete "
    "record: what the user asked, which files/commands were involved, key "
    "findings, decisions made, and any state needed to continue the work. "
    "Preserve concrete identifiers (file paths, function names, error text). "
    "Output only the summary, with no preamble."
)


def _content_chars(content: Any) -> int:
    """Char count of a message's content across both API shapes."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                # Anthropic text / tool_result / tool_use blocks.
                if isinstance(block.get("text"), str):
                    total += len(block["text"])
                c = block.get("content")
                if isinstance(c, str):
                    total += len(c)
                elif isinstance(c, list):
                    total += _content_chars(c)
                if isinstance(block.get("input"), (dict, list)):
                    total += len(json.dumps(block["input"], ensure_ascii=False))
        return total
    return len(str(content))


def _message_chars(msg: dict[str, Any]) -> int:
    total = _content_chars(msg.get("content"))
    # OpenAI assistant tool_calls carry JSON-encoded argument strings.
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments", "")
        total += len(args) if isinstance(args, str) else len(json.dumps(args))
    return total


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate for a message list (char/4 heuristic)."""
    return sum(_message_chars(m) for m in messages) // CHARS_PER_TOKEN


def _is_tool_result(msg: dict[str, Any]) -> bool:
    """True if msg is a tool-result message in either API shape.

    OpenAI: role == "tool". Anthropic: role == "user" whose content is a list
    whose first block is a tool_result. Used to keep cut boundaries clean — the
    kept tail must never start with an orphaned tool_result.
    """
    if msg.get("role") == "tool":
        return True
    if msg.get("role") == "user":
        content = msg.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "tool_result":
                return True
    return False


def _is_environment(source: str | None) -> bool:
    """True when a user message's source marks it as an environment injection."""
    return source in ENV_SOURCES


def _split_head(
    messages: list[dict[str, Any]], sources: list[str | None] | None = None,
) -> tuple[list[dict], int]:
    """Preserve the system message, any leading environment injections, and the
    first human message (the original-task anchor).

    Returns (head, index-after-head). Environment injections (memory/skills/mode)
    precede the human message (H ordering), so they are kept in the head to stay
    contiguous; the first non-injection user message is the anchor that ends the
    head. When ``sources`` is None (no session log), every non-tool user message
    is treated as human.
    """
    head: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    if i < n and messages[i].get("role") == "system":
        head.append(messages[i])
        i += 1
    while i < n:
        m = messages[i]
        if m.get("role") != "user" or _is_tool_result(m):
            break  # first non-user message — the middle begins
        src = sources[i] if sources is not None and i < len(sources) else None
        head.append(m)
        i += 1
        if not _is_environment(src):
            break  # first human message — the anchor
    return head, i


def _tail_start(messages: list[dict[str, Any]], head_end: int, budget_tokens: int) -> int:
    """Index where the preserved recent tail begins.

    Walks back from the end accumulating until ~budget_tokens, then advances
    forward past any leading tool-result so the tail never starts orphaned.
    """
    acc = 0
    start = len(messages)
    for i in range(len(messages) - 1, head_end - 1, -1):
        acc += _message_chars(messages[i]) // CHARS_PER_TOKEN
        start = i
        if acc >= budget_tokens:
            break
    # Don't begin the tail on an orphaned tool-result (its assistant call would
    # be in the dropped/summarized middle). Also skip an assistant message that
    # carries tool_calls whose results would land in the tail but whose call we
    # keep — that's fine; the risk is only a LEADING tool_result.
    while start < len(messages) and _is_tool_result(messages[start]):
        start += 1
    return start


async def compact(
    messages: list[dict[str, Any]],
    window: int,
    provider: Any,
    *,
    threshold: float = 0.8,
    tail_fraction: float = 0.3,
    sources: list[str | None] | None = None,
) -> tuple[list[dict[str, Any]], bool, tuple[int, int, list[dict[str, Any]]] | None]:
    """Compact an over-budget message list into a single region-replace.

    Returns ``(messages, did_compact, edit)``. ``edit`` is
    ``(start, end, replacement)`` — the replaced message-index range
    ``[start, end)`` of the ORIGINAL ``messages`` and its replacement — or
    ``None`` when nothing changed. The middle region between the preserved head
    (system + leading environment injections + first human message) and the
    recent tail is collapsed into one summary message; a failed summarize falls
    back to a truncation note. ``sources`` is parallel to ``messages`` and marks
    each message's ``user/message`` source so the anchor skips injections.
    """
    limit = int(threshold * window)
    if estimate_tokens(messages) <= limit:
        return messages, False, None

    head, head_end = _split_head(messages, sources)
    tail_budget = int(tail_fraction * window)
    tstart = _tail_start(messages, head_end, tail_budget)
    if tstart <= head_end:
        # Nothing in the middle to compact (tail already spans everything).
        return messages, False, None
    tail = messages[tstart:]
    middle = messages[head_end:tstart]

    try:
        summary_text = await _summarize(middle, provider)
        replacement = [{
            "role": "user",
            "content": f"[Earlier conversation summary]\n{summary_text}",
        }]
    except Exception:
        replacement = [{
            "role": "user",
            "content": "[Earlier conversation truncated to fit the context window.]",
        }]

    return head + replacement + tail, True, (head_end, tstart, replacement)


async def _summarize(middle: list[dict[str, Any]], provider: Any) -> str:
    """One-shot summary of the middle region. Raises on any provider failure."""
    serialized = _serialize_for_summary(middle)
    resp = await provider.chat(
        [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": serialized},
        ],
        [],  # no tools during summarization
    )
    text = (resp.text or "").strip()
    if not text:
        raise ValueError("empty summary")
    return text


def _serialize_for_summary(messages: list[dict[str, Any]]) -> str:
    """Flatten messages to plain text for the summarizer prompt."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):
            body = content
        elif isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if isinstance(b.get("text"), str):
                        parts.append(b["text"])
                    elif isinstance(b.get("content"), str):
                        parts.append(b["content"])
            body = "\n".join(parts)
        else:
            body = str(content or "")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            body += f"\n[tool call: {fn.get('name', '?')} {fn.get('arguments', '')}]"
        lines.append(f"{role}: {body}")
    return "\n\n".join(lines)
