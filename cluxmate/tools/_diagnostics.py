"""Post-write LSP diagnostics appended to the file tools' results (P0-5).

A write tool's result text is the model's only feedback on its own edit, and it
says nothing about whether the edit left the file broken. When a language server
is running for the file's language, its ERROR diagnostics are appended to the
result, so a typo the model just introduced comes back in the same step instead
of surfacing much later in a failing test (or not at all).

The manager side (``LSPManager.auto_diagnostics``) owns the policy — errors
only, no implicit install, "" when quiet. This module owns the tool-side shape:
how many files one call reports on, and how the block is framed.

Nothing here is allowed to affect the write: the write has already succeeded and
been reported, so a diagnostics failure must be invisible.
"""

import asyncio
from typing import Any

# Files reported per call. A batch edit can touch up to 20 files and each check
# can wait for a server's diagnostics push, so the report is bounded to the
# first few: enough to catch the mistake, without serializing the whole batch
# behind language servers.
MAX_FILES = 3

HEADER = "LSP errors detected in this file, please fix:"


async def diagnostics_suffix(manager: Any, paths: list[str]) -> str:
    """``"\\n\\nLSP errors detected ..."`` for the written files, or ``""``.

    ``manager`` is the session's LSPManager (None → feature off, e.g. a tool
    constructed without one). Duplicate paths are collapsed and the checks are
    capped at :data:`MAX_FILES`.
    """
    if manager is None or not paths:
        return ""
    blocks: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        if len(seen) >= MAX_FILES:
            break
        seen.add(path)
        block = await _auto_diagnostics(manager, path)
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return "\n\n" + HEADER + "\n" + "\n".join(blocks)


async def _auto_diagnostics(manager: Any, path: str) -> str:
    """Run the (blocking, possibly slow) manager query off the event loop."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, manager.auto_diagnostics, path)
    except Exception:
        return ""
