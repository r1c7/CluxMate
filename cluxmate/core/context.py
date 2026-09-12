"""Context window management — estimate size and compact when over budget.

The agent loop accumulates messages across turns and tool round-trips with no
bound. compact() shrinks an over-budget message list while preserving API
validity: an assistant message carrying tool_calls must keep its paired
tool_results, and the kept recent tail must not begin with an orphaned
tool_result (both Anthropic and OpenAI 400 otherwise).

Both cut points are therefore *tool-pairing balanced* cuts — positions where no
tool call is left unanswered across the cut (DSH ``tool-pairing.ts``,
Grok ``select.rs``). The tail cut is snapped to the nearest balanced position,
preferring the one that keeps the most recent call/result group in the
preserved tail: that group is the freshest context the model has, and snapping
the other way summarizes it away — at the extreme leaving an empty tail.

Compaction is a single region-replace: the middle between the preserved head
(system + first user message) and the recent tail is collapsed into one summary
message. The caller records that replacement as a session-log surface op so the
compacted transcript stays replayable (see ``AgentLoop._log_compaction``), and
guards it with the log's surface generation — the summarizer awaits, so the
surface it was measured against must still be the surface it is applied to. A
failed summarize falls back to a truncation note so a turn never hard-fails.

Token counts are char/4 estimates (no tokenizer dependency); the agent loop
calibrates against the provider's real usage where available.
"""

import json
from typing import Any

CHARS_PER_TOKEN = 4

# ``user/message`` sources that are environment injections (memory/memory-recall/
# skills/mode/compaction/interruption/hook), not human turns. ``_split_head`` skips them when
# finding the original-task anchor so the head starts at the first HUMAN message.
ENV_SOURCES = frozenset({"memory", "memory-recall", "skill", "mode", "compaction", "interruption", "hook"})

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


def _unanswered_delta(msg: dict[str, Any]) -> int:
    """Net change in unanswered tool calls contributed by one message.

    OpenAI declares calls on the assistant message (``tool_calls``) and answers
    each with a ``role: "tool"`` message; the Anthropic shape uses ``tool_use`` /
    ``tool_result`` content blocks. Mirrors DSH ``tool-pairing.eventDelta``.
    """
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return len(tool_calls)
    content = msg.get("content")
    if isinstance(content, list):
        kinds = [b.get("type") for b in content if isinstance(b, dict)]
        calls = kinds.count("tool_use")
        if calls:
            return calls
        results = kinds.count("tool_result")
        if results:
            return -results
    if msg.get("role") == "tool":
        return -1
    return 0


def _balance_prefix(messages: list[dict[str, Any]]) -> list[int]:
    """``balance[i]`` — unanswered tool calls immediately BEFORE message ``i``.

    A cut at index ``i`` is *balanced* when ``balance[i] == 0``: no tool call is
    left open across it, so an assistant message carrying ``tool_calls`` always
    keeps its paired results on the same side of the cut. The last entry is the
    balance after the final message.
    """
    balance = [0] * (len(messages) + 1)
    for i, msg in enumerate(messages):
        balance[i + 1] = balance[i] + _unanswered_delta(msg)
    return balance


def _snap_forward(balance: list[int], index: int) -> int:
    """First balanced cut at or after ``index`` (``index`` when there is none)."""
    for i in range(index, len(balance)):
        if balance[i] == 0:
            return i
    return index


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


def _tail_target(
    messages: list[dict[str, Any]], head_end: int, budget_tokens: int
) -> int:
    """Unsnapped index where a ~``budget_tokens`` recent tail would begin.

    Walks back from the end accumulating until the budget is spent. The result is
    a size estimate only — :func:`_pairing_cut` moves it to a legal cut.
    """
    acc = 0
    start = len(messages)
    for i in range(len(messages) - 1, head_end - 1, -1):
        acc += _message_chars(messages[i]) // CHARS_PER_TOKEN
        start = i
        if acc >= budget_tokens:
            break
    return start


def _pairing_cut(
    messages: list[dict[str, Any]],
    head_end: int,
    target: int,
    balance: list[int],
) -> int:
    """Move ``target`` to a tool-pairing-balanced tail cut (:func:`_balance_prefix`).

    Preference order:

    1. ``target`` itself when it is already balanced.
    2. **Backward** to the cut that opens the call/result group ``target`` fell
       inside. That group is the freshest context in the conversation and belongs
       in the preserved tail; snapping forward instead would summarize the tool
       results the model just asked for — and when the walk lands inside the last
       group, forward snapping leaves an *empty* tail, i.e. the whole recent
       conversation becomes a summary. Overshoot is bounded by that one group.
    3. **Forward** past the group, the only option when backward would leave
       nothing to summarize (the group starts at the head boundary).
    4. A malformed transcript with no balanced cut nearby (a result with no call)
       degrades to the shape rule this module always had — never begin the tail
       on a tool-result — instead of raising.
    """
    if balance[target] == 0:
        return target
    back = target
    while back > head_end and balance[back] != 0:
        back -= 1
    if back > head_end and balance[back] == 0:
        return back
    fwd = target
    while fwd < len(messages) and balance[fwd] != 0:
        fwd += 1
    if balance[fwd] == 0:
        return fwd
    while fwd < len(messages) and _is_tool_result(messages[fwd]):
        fwd += 1
    return fwd


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

    Both boundaries are tool-pairing balanced cuts, so the returned list is a
    valid transcript (see :func:`_pairing_cut`).
    """
    limit = int(threshold * window)
    if estimate_tokens(messages) <= limit:
        return messages, False, None

    head, head_end = _split_head(messages, sources)
    balance = _balance_prefix(messages)
    # The head must not end inside a call group either: advance the cut past the
    # group rather than let the summarized region open on a bare tool-result.
    snapped_head_end = _snap_forward(balance, head_end)
    if snapped_head_end != head_end:
        head_end = snapped_head_end
        head = messages[:head_end]
    tail_budget = int(tail_fraction * window)
    tstart = _pairing_cut(
        messages, head_end, _tail_target(messages, head_end, tail_budget), balance
    )
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
