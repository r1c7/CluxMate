"""Bound oversized tool output without silently discarding the middle.

The old policy cut a result at ``MAX_OUTPUT_CHARS`` and dropped everything
after it — single-ended, so the END of a long result is exactly what vanished:
a traceback's last frames, a test runner's summary, a subagent's ``**Status**``
line. Two layers replace it, applied in order by :func:`bound_output`:

1. **head/tail truncation** — the retained budget is split between the start
   and the end of the text, and the dropped middle is replaced by an explicit
   ``[... N characters omitted ...]`` marker. The split is symmetric unless the
   tail looks like a failure (``error|traceback|panic`` in the last
   :data:`ERROR_TAIL_SCAN_CHARS` characters), in which case it shifts toward the
   tail so the diagnosis survives.
2. **spill** — when a working directory is known, the FULL text is also written
   under ``<cwd>/.cluxmate/tmp-spill/`` and the model-facing result carries that
   path plus a read-back hint, so nothing is unrecoverable.

Spill invariants (same lessons as DSH's spill policy):

- **Best-effort**: any failure to save (permissions, ENOSPC, no directory)
  falls back to plain inline truncation. A spill failure must NEVER turn a
  successful tool call into an error result or hide the preview.
- **``read_file`` is skipped** — otherwise a spilled read invites reading the
  spill file, which spills again.
- **Retention**: spill files are scratch, not state. Files older than
  :data:`SPILL_RETENTION_DAYS` are deleted lazily on the next spill write; no
  background thread is spawned.
- **Bounded**: the replacement (preview + notice) stays within the caller's
  cap, so spilling can never produce a result LARGER than plain truncation.

``<cwd>/.cluxmate/`` is CluxMate's own project state (denied to the model's
write tools by ``WriteFence``, gitignored on CluxMate's own repo), so scratch
output there is readable by ``read_file``/``grep`` but never writable by the
model.
"""

import os
import re
import time
import uuid
from pathlib import Path

# Failure-looking text near the END of a result shifts the split toward the
# tail (where the diagnosis is). Scanned over the last ERROR_TAIL_SCAN_CHARS.
ERROR_PATTERN = re.compile(r"error|traceback|panic", re.IGNORECASE)
ERROR_TAIL_SCAN_CHARS = 2048
ERROR_TAIL_FRACTION = 0.7

# Directory (under <cwd>/.cluxmate/) holding spilled full outputs, and the age
# after which they are swept. Scratch, not state: losing one only costs the
# model a re-run of the tool that produced it.
SPILL_DIR_NAME = "tmp-spill"
SPILL_RETENTION_DAYS = 7
SPILL_SWEEP_MAX_ENTRIES = 500

# Tools whose own output must never be spilled: read_file already gives the
# model offset/limit, and spilling a read invites read -> spill -> read again.
SPILL_SKIPPED_TOOLS = frozenset({"read_file"})

# Preview cuts prefer a line boundary, but only when the partial line left
# behind is short: a single huge line (minified JSON, a base64 blob) must not
# cost the preview nearly its whole budget.
LINE_TRIM_MAX_CHARS = 200


def head_tail(text: str, budget: int) -> tuple[str, str, int]:
    """Split ``text`` into (head, tail, omitted_chars) within ``budget`` chars.

    The two ends are cut at line boundaries when that costs at most
    :data:`LINE_TRIM_MAX_CHARS` characters, so a preview normally never starts
    or ends mid-line. ``omitted_chars`` counts exactly what was dropped,
    including anything trimmed at the cut points.
    """
    if budget <= 0:
        return "", "", len(text)
    if budget >= len(text):
        return text, "", 0
    scan = text[-ERROR_TAIL_SCAN_CHARS:] if len(text) > ERROR_TAIL_SCAN_CHARS else text
    if ERROR_PATTERN.search(scan):
        tail_budget = int(budget * ERROR_TAIL_FRACTION)
    else:
        tail_budget = budget // 2
    head_budget = budget - tail_budget
    head, tail = text[:head_budget], text[len(text) - tail_budget:]
    cut = head.rfind("\n")
    if cut > 0 and len(head) - cut <= LINE_TRIM_MAX_CHARS:
        head = head[:cut]
    cut = tail.find("\n")
    if 0 <= cut < len(tail) - 1 and cut + 1 <= LINE_TRIM_MAX_CHARS:
        tail = tail[cut + 1:]
    return head, tail, len(text) - len(head) - len(tail)


def bound_output(
    text: str, *, cwd: str | None, tool_name: str, max_chars: int
) -> str:
    """Return ``text`` unchanged when it fits, else a bounded head/tail view.

    A full copy is spilled to ``<cwd>/.cluxmate/tmp-spill/`` when possible; the
    returned string then points at it. Never raises: every filesystem problem
    degrades to the inline truncation.
    """
    if len(text) <= max_chars:
        return text
    path = None
    if tool_name not in SPILL_SKIPPED_TOOLS:
        path = _try_spill(text, cwd, tool_name)
    return _project(text, max_chars, path)


def _project(text: str, max_chars: int, path: Path | None) -> str:
    """Build the bounded replacement (preview + omission notice)."""
    # Price the notice with the WORST-CASE omission count (the whole text, whose
    # digit count bounds the real one), so the finished replacement — preview
    # plus joins plus notice — can never exceed max_chars.
    reserve = len(_notice(len(text), path)) + 4
    budget = max_chars - reserve
    if budget <= 0:
        # Degenerate cap (smaller than the notice itself): keep a minimal view
        # rather than an empty result. Unreachable with the real 40k cap.
        budget = max_chars // 2
    head, tail, omitted = head_tail(text, budget)
    parts = [part for part in (head, _notice(omitted, path), tail) if part]
    return "\n\n".join(parts)


def _notice(omitted: int, path: Path | None) -> str:
    if path is None:
        return f"[... {omitted} characters omitted ...]"
    return (
        f"[... {omitted} characters omitted — full output saved to {path}]\n"
        "Use read_file (offset/limit) to read a section of that file, "
        "or grep to search it."
    )


def _safe_name(tool_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)[:40] or "tool"


def _try_spill(text: str, cwd: str | None, tool_name: str) -> Path | None:
    """Best-effort write of the full text; None when it cannot be saved."""
    if not cwd:
        return None
    try:
        directory = Path(cwd) / ".cluxmate" / SPILL_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        _sweep(directory)
        path = directory / f"{_safe_name(tool_name)}-{time.time_ns()}-{uuid.uuid4().hex[:6]}.txt"
        # Byte I/O (not write_text) so the spilled copy is exactly the tool's
        # text — no platform newline translation, same rule as _fileio.
        path.write_bytes(text.encode("utf-8"))
        return path
    except Exception:
        return None


def _sweep(directory: Path, *, now: float | None = None) -> None:
    """Delete spill files older than the retention window (bounded, best-effort)."""
    cutoff = (time.time() if now is None else now) - SPILL_RETENTION_DAYS * 86_400
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= SPILL_SWEEP_MAX_ENTRIES:
                    break
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        return
