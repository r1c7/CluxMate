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
  "done". Negated claims ("did not modify", "couldn't run") are skipped, and a
  change verb only counts as a WORD whose subject can be this agent — which is
  what keeps a review report's prose ("the `implementer` gets stuck in
  `test_pack.py`", "this commit edited the import group", a `--patch` flag next
  to a path) from reading as a claim about its own work.
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
# The English alternatives must be WORDS: without boundaries `implementer`,
# `StrategyPatch` and `create_app` read as verbs, and so does the `--patch` CLI
# flag next to a path — a review report is made of exactly that prose, and every
# false positive measured in one five-round review session came from one of
# these or from a verb whose subject was somebody else (see below).
_ASCII_CHANGE_VERB = (
    r"fix(?:ed)?|implement(?:ed)?|creat(?:ed|e)|add(?:ed)?|updat(?:ed|e)|"
    r"modif(?:ied|y)|chang(?:ed|e)|edit(?:ed)?|remov(?:ed|e)|delet(?:ed|e)|"
    r"wrot(?:e|ten)|generat(?:ed|e)|refactor(?:ed)?|patch(?:ed)?"
)
# CJK words have no boundaries: a boundary class would reject 已修改, where 已
# is itself a word character. The Chinese alternatives stay unbounded.
_CJK_CHANGE_VERB = r"修复|实现|创建|添加|删除|更新|修改|生成|重构|写好|改好"
_CHANGE_VERB = re.compile(
    r"(?:(?<![\w\-./])(?:%s)(?![\w\-])|(?:%s))"
    % (_ASCII_CHANGE_VERB, _CJK_CHANGE_VERB),
    re.IGNORECASE,
)

# A change verb whose *subject* is not this agent: a report narrating somebody
# else's change or restating the requirement ("this commit edited the import
# group", "the plan must create pack.py", "Requirement C4: add evaluator.py") is
# not a completion claim. An actor word anywhere in the short prefix counts,
# UNLESS that prefix also carries a first person — which is what keeps a real
# claim in the same sentence ("the plan said to fix foo.py; I fixed foo.py")
# firing on its own occurrence. `_claimed_files` scans EVERY verb in the window
# for exactly that reason: one report sentence often contains both kinds.
_THIRD_PARTY_ACTOR = re.compile(
    r"(?:commit|commits|plan|plans|spec|specs|requirement|requirements|review|"
    r"reviews|round|pass|passes|diff|diffs|author|host|audit|audits|task|tasks|"
    r"step|steps|pr|prs)\b",
    re.IGNORECASE,
)
# The agent's own voice. Deliberately narrow: "we/us/our" are how a document is
# described ("the spec asks us to implement retriever.py"), not how this agent
# states what it did.
_FIRST_PERSON = re.compile(r"\b(?:I|my)\b|我", re.IGNORECASE)
# How far an actor word may sit from the verb and still be read as its subject.
_ACTOR_GAP = 25
# How much text before the verb is read as its subject phrase.
_ACTOR_WINDOW = 40

# A modal in front of the verb is intent, not completion ("the plan says I
# should create util.py"). Bare `to` is deliberately NOT here: "I managed to fix
# util.py" is a completion claim.
_NON_ASSERTIVE_MODAL = re.compile(
    r"(?:must|should|shall|will|would|can|could|may|might)[ \t]+\Z",
    re.IGNORECASE,
)

# A determiner in front of the change verb makes it a NOUN PHRASE, not a
# predicate: "the fix to utils.py is missing", "the change to evaluator.py is
# correct", "a patch to the ids". Those are how a report talks ABOUT a change
# (and the shape AGENTS.md already documented as a false positive), while a real
# claim puts the verb behind a subject ("I fixed utils.py").
_NOUN_PHRASE = re.compile(
    r"(?:\b(?:the|a|an|this|that|these|those|any|each|no|its|their|his|her|"
    r"our|your|my|one|another|such|same)[ \t]+)\Z",
    re.IGNORECASE,
)

# Passive voice right after the verb ("it is added by this commit"): the change
# is predicated of something other than the agent.
_PASSIVE_BY = re.compile(r"\s+by\b")

# Inline-code and double-quoted spans on one line. A change verb inside one is
# quoted material — a plan bullet such as `- Create: wiring.py`, or a report
# sentence quoting one ("the \"Create wiring.py / Test test_cli.py\" bullet
# merge") — not the agent's own assertion. The double-quoted form is
# length-capped: prose quotes are often unbalanced, and an unbounded span could
# swallow a real claim. The cap is generous (a report quotes whole sentences),
# and the alternatives are tried in this order so an inline-code span is never
# cut short by an unrelated double quote inside it.
_QUOTED_SPAN = re.compile(r"`[^`\n]*`|\"[^\"\n]{0,300}\"")

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
    # "never" only as a denial of a change ("I never claimed to change X"): a
    # bare `never` would also swallow a real claim ("I never gave up and fixed X").
    r"never\s+(?:\w+\s+){0,3}to[ \t]+\Z|"
    r"没有|并未|还没|尚未|无法|不能|从未|未)",
    re.IGNORECASE,
)

# How far (chars) a claim verb may sit from a file/command token to count as
# one claim. Keeps "I read foo.py and did not change it" from firing when
# there is no negation word immediately adjacent.
_CLAIM_WINDOW = 80


def normalize_path(path: str) -> str:
    """Comparable form of a tool-reported path: basename, lowercased, unquoted.

    Quote characters are stripped because ``_FILE_TOKEN`` matches an optional
    opening backtick/quote: without this, a claim written as `` `utils.py `` can
    never equal the write record's ``utils.py``, and the backup path check
    bounces a claim it should have been able to back.
    """
    token = path.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return token.strip("`\"'").lower()


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


def _is_third_party(text: str, verb_start: int) -> bool:
    """True when the verb is not an assertion of what THIS agent did.

    Either the verb sits in somebody else's sentence — an actor word within
    ``_ACTOR_GAP`` of it, with no first person in the prefix — or it is intent
    rather than completion (``_NON_ASSERTIVE_MODAL``).
    """
    lo = max(0, verb_start - _ACTOR_WINDOW)
    window = text[lo:verb_start]
    if _NON_ASSERTIVE_MODAL.search(window):
        return True
    if _FIRST_PERSON.search(window):
        return False
    actor = _THIRD_PARTY_ACTOR.search(window)
    if actor is None:
        return False
    return verb_start - (lo + actor.end()) <= _ACTOR_GAP


def _is_quoted(spans: list[tuple[int, int]], pos: int) -> bool:
    """True when ``pos`` falls inside a quoted span (quoted material)."""
    return any(start < pos < end for start, end in spans)


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
    spans = [(m.start(), m.end()) for m in _QUOTED_SPAN.finditer(text)]
    for m in _FILE_TOKEN.finditer(text):
        lo = max(0, m.start() - _CLAIM_WINDOW)
        hi = min(len(text), m.end() + _CLAIM_WINDOW)
        window = text[lo:hi]
        # EVERY verb in the window, not just the first: a report sentence
        # routinely carries both a quoted/third-party verb and the agent's own
        # claim ("the plan said to fix foo.py; I fixed foo.py"), and only one of
        # them decides the verdict.
        for verb in _CHANGE_VERB.finditer(window):
            verb_start = lo + verb.start()
            if _is_negated(text, verb_start) or _is_third_party(text, verb_start):
                continue
            if _is_quoted(spans, verb_start):
                continue
            if _PASSIVE_BY.match(text, lo + verb.end()):
                continue
            claimed[normalize_path(m.group(0))] = (
                "deletion" if _DELETE_VERB.search(window) else "change"
            )
            break
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
