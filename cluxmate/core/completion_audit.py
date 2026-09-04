"""Turn-end completion audit — claim-vs-evidence reconciliation.

The session log records every tool call and result, but the agent loop accepts
any ``end_turn`` reply at face value. This module closes the gap for the cheap,
deterministic cases: a final reply that claims work (file changes, a command or
test outcome) that this turn's tool calls cannot support.

Design constraints for CluxMate:
- The audit is ADVISORY. It returns a reminder string; the loop injects it as a
  synthetic user message and re-runs, bounded (mirrors the Stop-hook block path
  and the doom-loop reminder). It never hard-blocks a reply.
- Detection is deliberately conservative: it fires only on strong signals (a
  change/test claim paired with a file or command token), never on a bare
  "done". Negated claims ("did not modify", "couldn't run") are skipped.
- When a bash call DID run this turn, test-outcome claims get the benefit of
  the doubt (no command-semantics matching). File claims are then checked
  against the FILESYSTEM when the caller supplies a ``resolve_touched``
  callback (mtime vs turn start); without one, the path-level checks are
  skipped and bash turns pass unverified.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Iterable

# Tools whose executed calls are the only host-verifiable evidence for
# "I changed file X" claims. bash-based edits are excluded by design (the
# system prompt forbids echo/rm for file mutations; a bash edit claim without
# a matching write call is exactly the case the audit should catch — but when
# bash ran at all, path-level matching is skipped to stay conservative).
WRITE_TOOLS = frozenset(
    {"write_file", "search_replace", "multi_edit", "multi_write", "delete_file"}
)

_FILE_EXT = (
    r"py|ts|tsx|js|jsx|mjs|cjs|rs|go|java|c|cpp|h|hpp|cs|rb|php|sh|ps1|bat|"
    r"md|txt|rst|json|ya?ml|toml|ini|cfg|html|css|scss|sql|vue|svelte|"
    r"ipynb|lock|env|mk|cmake|makefile|dockerfile"
)
# A file-looking token: optionally backtick/quoted, path-ish chars, known ext.
_FILE_TOKEN = re.compile(
    r"[`\"']?[\w./\\:@\-]+\.(?:%s)\b" % _FILE_EXT, re.IGNORECASE
)

_CLAIM_VERBS_EN = (
    r"done|finish(?:ed)?|complet(?:ed|e)|fix(?:ed)?|implement(?:ed)?|"
    r"creat(?:ed|e)|add(?:ed)?|updat(?:ed|e)|modif(?:ied|y)|chang(?:ed|e)|"
    r"edit(?:ed)?|remov(?:ed|e)|delet(?:ed|e)|wrot(?:e|ten)|generat(?:ed|e)|"
    r"refactor(?:ed)?|patch(?:ed)?|install(?:ed)?|succeed(?:ed)?|success|"
    r"resolv(?:ed|e)|pass(?:ed)?|work(?:s|ing)?|verif(?:ied|y)|test(?:ed)?|"
    r"build|compil(?:ed|e)|ran|run"
)
_CLAIM_VERBS_CN = (
    r"完成|已修复|已实现|已修改|已创建|已添加|已删除|已更新|已生成|已重构|"
    r"搞定|做好|写好|改好|通过|成功|验证|测试|运行|编译|构建"
)
_CLAIM_VERB = re.compile(
    r"(?:%s)" % "|".join((_CLAIM_VERBS_EN, _CLAIM_VERBS_CN)), re.IGNORECASE
)

# Change verbs (subset of claim verbs) that support a *file-change* claim.
_CHANGE_VERB = re.compile(
    r"(?:fix(?:ed)?|implement(?:ed)?|creat(?:ed|e)|add(?:ed)?|updat(?:ed|e)|"
    r"modif(?:ied|y)|chang(?:ed|e)|edit(?:ed)?|remov(?:ed|e)|delet(?:ed|e)|"
    r"wrot(?:e|ten)|generat(?:ed|e)|refactor(?:ed)?|patch(?:ed)?|"
    r"修复|实现|创建|添加|删除|更新|修改|生成|重构|写好|改好)",
    re.IGNORECASE,
)

# Command/test-outcome tokens: "the tests passed" / "build is green".
_TEST_VERB = re.compile(
    r"(?:tests?|testing|pytest|unittest|lint|typecheck|tsc|mypy|eslint|"
    r"cargo test|go test|npm test|npm run build|build|compile|"
    r"测试|编译|构建|冒烟|回归)",
    re.IGNORECASE,
)
_PASS_VERB = re.compile(
    r"(?:pass(?:ed|es|ing)?|succe(?:ed|ss|ssful)|green|works?|fine|"
    r"通过|成功|正常|绿色)",
    re.IGNORECASE,
)

# A negated claim ("I did NOT fix foo.py") is an honest non-completion, not a
# fake one. Check a small window BEFORE the verb only — pattern-order quirks
# in long sentences are acceptable for an advisory audit.
_NEGATION = re.compile(
    r"(?:did not|didn'?t|does not|doesn'?t|cannot|can'?t|could not|couldn'?t|"
    r"not |no |without|unable|failed to|"
    r"没有|并未|还没|尚未|无法|不能|未)",
    re.IGNORECASE,
)

# How far (chars) a claim verb may sit from a file/command token to count as
# one claim. Keeps "I read foo.py and did not change it" from firing when
# there is no negation word immediately adjacent.
_CLAIM_WINDOW = 80


def normalize_path(path: str) -> str:
    """Comparable form of a tool-reported path: basename, lowercased."""
    return path.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def tool_write_paths(name: str, args: dict) -> list[str]:
    """File paths a write-tool call targets, from its input schema.

    ``write_file``/``search_replace``/``delete_file`` take ``path``;
    ``multi_edit`` takes ``edits[].path``; ``multi_write`` takes
    ``files[].path``.
    """
    if name not in WRITE_TOOLS:
        return []
    if name == "multi_edit":
        return [e.get("path", "") for e in (args or {}).get("edits", [])]
    if name == "multi_write":
        return [f.get("path", "") for f in (args or {}).get("files", [])]
    return [(args or {}).get("path", "")]


# Directories skipped by the workspace scan — vendored/derived/build output
# trees that cannot legitimately be edit targets and would blow the budget.
_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox",
    "dist", "build", "target", ".cluxmate", ".pytest_cache", ".mypy_cache",
    ".ruff_cache",
})
_SCAN_FILE_BUDGET = 5000


def resolve_file_touched(name: str, cwd: str, turn_start_ts: float) -> bool | None:
    """Whether a claimed file was modified during this turn, per the filesystem.

    Returns True (some matching file's mtime is >= the turn start), False (the
    file exists untouched, or does not exist anywhere in the workspace), or
    None when the scan budget ran out before the name was found (unknown —
    callers should then skip the check rather than bounce).

    ``name`` may be an absolute path, a relative path, or a bare basename
    (extracted from the reply). This is the ground-truth complement to the
    tool-call record: bash edits leave no tool-level trace of WHAT changed,
    but the filesystem does.
    """
    token = name.strip().strip("`\"'")
    if not token:
        return None

    def _touched(path: str) -> bool | None:
        try:
            st = os.stat(path)
        except OSError:
            # Does not exist — an "I fixed X" claim about a file that is not
            # on disk is not backed by the filesystem.
            return False
        return st.st_mtime >= turn_start_ts

    if os.path.isabs(token):
        return _touched(token)
    joined = os.path.join(cwd, token)
    if "/" in token or "\\" in token or os.path.exists(joined):
        return _touched(joined)
    # Bare basename: scan the workspace (bounded, skipping heavy trees).
    target = token.casefold()
    scanned = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d.casefold() not in _SCAN_SKIP_DIRS]
        for f in files:
            scanned += 1
            if scanned > _SCAN_FILE_BUDGET:
                return None
            if f.casefold() == target:
                return _touched(os.path.join(root, f))
    return False


def _is_negated(text: str, claim_start: int) -> bool:
    window = text[max(0, claim_start - 30): claim_start]
    return bool(_NEGATION.search(window))


# Delete/remove verbs — a "deleted utils.py" claim cannot be cheaply verified
# against mtime (the file is gone either way), so the fs check skips it.
_DELETE_VERB = re.compile(
    r"(?:remov(?:ed|e)?|delet(?:ed|e)?|删除|移除)", re.IGNORECASE
)


def _claimed_files(text: str) -> dict[str, str]:
    """``{basename: kind}`` — file claims in the reply paired with a change verb.

    ``kind`` is ``"deletion"`` when a delete/remove verb sits in the claim
    window, else ``"change"``.
    """
    claimed: dict[str, str] = {}
    for m in _FILE_TOKEN.finditer(text):
        lo = max(0, m.start() - _CLAIM_WINDOW)
        hi = min(len(text), m.end() + _CLAIM_WINDOW)
        window = text[lo:hi]
        verb = _CHANGE_VERB.search(window)
        if verb is None:
            continue
        if _is_negated(text, lo + verb.start()):
            continue
        name = normalize_path(m.group(0))
        kind = "deletion" if _DELETE_VERB.search(window) else "change"
        claimed[name] = kind
    return claimed


def _claims_test_outcome(text: str) -> bool:
    """True when the reply pairs a test/command token with a pass verb."""
    for m in _TEST_VERB.finditer(text):
        lo = max(0, m.start() - _CLAIM_WINDOW)
        hi = min(len(text), m.end() + _CLAIM_WINDOW)
        window = text[lo:hi]
        pm = _PASS_VERB.search(window)
        if pm is None:
            continue
        # Negation next to either the test token ("无法测试") or the pass verb
        # ("tests did not pass") marks an honest non-completion.
        if _is_negated(text, m.start()) or _is_negated(text, lo + pm.start()):
            continue
        return True
    return False


def audit_completion(
    text: str,
    *,
    write_paths: Iterable[str] | None = None,
    any_bash: bool = False,
    tool_calls_made: int = 0,
    resolve_touched: Callable[[str], bool | None] | None = None,
) -> str | None:
    """Reconcile a final reply against this turn's executed tool calls.

    Returns a reminder string to feed back to the model (None = no issue).
    The reminder is advisory: the loop re-runs with it as a synthetic user
    message, bounded, and falls through to the original reply afterwards.

    Checks (strongest first):
    1. The reply claims file changes, but this turn executed NO tool calls at
       all — nothing could have changed.
    2. The reply claims file changes, but this turn executed no write-tool
       calls (reads/other tools ran) — or names paths the write calls did not
       touch. Skipped when a bash call ran (see module docstring).
    3. A bash call ran (tool record can't say WHAT changed): when
       ``resolve_touched`` is given, non-deletion file claims are checked
       against the filesystem (mtime vs turn start) and unbacked ones fire.
       Without ``resolve_touched`` this check is skipped.
    4. The reply claims a test/command outcome, but no bash call executed this
       turn — no command actually ran.
    """
    if not text:
        return None
    files = _claimed_files(text)
    no_tools = tool_calls_made <= 0 and not write_paths and not any_bash
    if files and no_tools:
        sample = ", ".join(sorted(files)[:3])
        return (
            "Completion audit: your reply claims changes to files "
            f"({sample}) but this turn executed no tool calls that could have "
            "changed them. The claim is unsupported by this turn's tool "
            "record. Either make the change with the file tools now and cite "
            "the tool results, or rewrite the reply to remove the claim and "
            "state what remains to be done."
        )
    touched = {normalize_path(p) for p in (write_paths or []) if p}
    if files and not any_bash:
        if not touched:
            sample = ", ".join(sorted(files)[:3])
            return (
                "Completion audit: your reply claims changes to files "
                f"({sample}) but this turn executed no write tool calls — "
                "only reads or other tools ran. A change claim must be backed "
                "by the write tool calls that actually happened. Either make "
                "the change now, or correct the reply."
            )
        missing = sorted(f for f in files if f not in touched)
        if missing:
            sample = ", ".join(missing[:3])
            return (
                "Completion audit: your reply claims changes to "
                f"{sample}, but this turn's write tool calls only touched "
                f"{sorted(touched)}. A claim must be backed by the tool calls "
                "that actually happened. Either change those files now, or "
                "correct the reply."
            )
    if files and any_bash and resolve_touched is not None:
        # Deletion claims are skipped: a deleted file looks identical to one
        # that never existed, so mtime cannot adjudicate them.
        unverified = sorted(
            name
            for name, kind in files.items()
            if kind == "change" and resolve_touched(name) is False
        )
        if unverified:
            sample = ", ".join(unverified[:3])
            return (
                "Completion audit: your reply claims changes to "
                f"{sample}, but the filesystem shows it was not modified "
                "during this turn (or it does not exist in the workspace). "
                "A bash call ran, but nothing on disk backs the claim. "
                "Either actually change the file now, or correct the reply."
            )
    if _claims_test_outcome(text) and not any_bash:
        return (
            "Completion audit: your reply claims a command or test outcome "
            "(e.g. tests passed, build green) but this turn executed no bash "
            "calls — no command actually ran. Run the verification now and "
            "quote its real output, or remove the claim and say it was not "
            "verified."
        )
    return None
