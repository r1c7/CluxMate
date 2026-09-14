"""Subagent type registry — built-in types plus user definitions on disk.

Definition roots (project overrides global, both override built-ins):

    ~/.cluxmate/agents/<slug>.md          # global
    <cwd>/.cluxmate/agents/<slug>.md      # project

A definition is markdown with a small frontmatter block (hand-parsed, no YAML
dependency — same posture as ``core/skills.py``):

    ---
    name: 代码审查员
    description: 审查一处改动是否正确、是否引入安全问题
    tools: [read_file, grep, list_dir, lsp]
    model: deepseek-v4-fast
    max_turns: 30
    subagents: none
    ---
    你是一名严格的代码审查员。……

Both roots live where the MODEL cannot write: ``~/.cluxmate`` sits outside every
WriteFence root, and ``<cwd>/.cluxmate`` is a WriteFence deny subtree. A
prompt-injected model therefore cannot mint itself a type with more tools.
`.claude/agents/` is deliberately NOT read (it lives inside the writable
workspace).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Default turn budget for a user-defined subagent. The built-in
# general-purpose type pins 150 to keep today's behavior unchanged.
DEFAULT_SUBAGENT_MAX_TURNS = 50
# Hard ceiling — AgentLoop.MAX_TURNS. Kept as a literal to avoid importing the
# agent module from the registry (it imports this one).
MAX_AGENT_TURNS = 150

# Tools that can change the workspace. A type holding any of these claims a
# write scope when it spawns (see core/subagent_scheduler.py); a type holding
# none is "read-only" and never serializes against anyone.
WRITE_TOOL_NAMES = frozenset({
    "bash", "write_file", "search_replace", "multi_edit", "multi_write", "delete_file",
})

# Every tool a subagent may hold. `task` is the recursion switch: without it a
# type cannot spawn further subagents.
SUBAGENT_TOOL_NAMES = frozenset({
    "bash", "read_file", "search_replace", "multi_edit", "write_file",
    "delete_file", "multi_write", "grep", "list_dir", "web_fetch", "web_search",
    "lsp", "task",
})

# Structurally parent-only tools (builder.py registers them at depth 0 only).
# Naming one is a definition error: silently ignoring it would leave the author
# believing it works.
PARENT_ONLY_TOOL_NAMES = frozenset({
    "ask_user_question", "todo_write", "update_memory", "remember", "forget",
    "use_skill",
})

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class AgentType:
    """One subagent type — built-in or user-defined."""

    slug: str
    description: str
    tools: tuple[str, ...]
    name: str = ""
    model: str = "inherit"          # "inherit" or a config.json model id
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS
    subagents_mode: str = "none"    # "none" | "inherit" | "list"
    subagents: tuple[str, ...] = ()
    instructions: str = ""          # markdown body → child system prompt
    builtin: bool = False
    source: str = "builtin"         # "builtin" | "global" | "project"
    path: str = ""

    @property
    def readonly(self) -> bool:
        """True when the type cannot change the workspace (never claims)."""
        return not (set(self.tools) & WRITE_TOOL_NAMES)


_READONLY_TOOLS = ("read_file", "grep", "list_dir", "web_fetch", "web_search", "lsp")

# The reviewer's toolset: enough to READ a change and RUN the commands that
# prove it (its contract demands a test name, command output or `file:line` per
# claim), and nothing that could make or fix the change itself. Holding `bash`
# therefore makes the type a "writer" for scheduling purposes (see
# WRITE_TOOL_NAMES) — deliberate: a reviewer must not race the edits it judges.
_REVIEW_TOOLS = ("read_file", "grep", "list_dir", "lsp", "bash")

# Reviewer contract. Adapted from MiMo-Code's spec-compliance gate
# (compose:subagent `spec-reviewer-prompt.md`): the reviewer is handed the
# requirement and the change, never the implementer's own account of it, and a
# verdict is only as good as the evidence attached to each claim. Two CluxMate
# adaptations: there are no worktrees, so "the change" is the working tree (the
# dispatcher names it in the prompt); and the verdict vocabulary is the child
# `**Status**:` line the host already parses (tools/task.py), not `pass|fail` —
# so an evidence-free `success` is still downgraded by the same machinery as any
# other subagent claim.
_REVIEWER_INSTRUCTIONS = """\
You are a spec-compliance reviewer. You judge whether work that is already done
matches what was asked for. You never do the work yourself: you hold no write
tools, and fixing what you find is not your job — reporting it is.

Work ONLY from the material you were given: the requirement text and the change
under review. There is deliberately no implementer summary; do not ask for one.
Such a summary would anchor you toward confirming what was reported and away
from finding what was silently omitted.

1. Enumerate the requirement's distinct, checkable CLAIMS. A claim is one
   verifiable statement about required behavior; a requirement usually holds
   several.
2. Mark each claim in scope or out of scope for THIS review, and judge only the
   in-scope ones.
3. Verify each in-scope claim against the actual change. A change shows what was
   added; it does NOT show what is missing. A required behavior with no
   corresponding code is a `fail` even though no line points at it — actively
   look for omissions, and for work nobody asked for.
4. Evidence is mandatory. A claim's status is backed by a test name, a command's
   real output, or a `file:line` reference. A status asserted without such
   evidence is `fail`. Prose like "looks implemented" is NOT evidence.
5. If a claim describes runtime behavior you cannot judge by reading, run the
   relevant test or command and quote its output. If it still cannot be
   verified, mark it `unverifiable` — never a silent pass.

Reply `**Status**: success` ONLY when every in-scope claim passes WITH evidence;
otherwise `**Status**: partial` (or `failed` when the work is fundamentally
wrong). Then one line per claim:

- [C1 <short claim>] in-scope - pass - evidence: <test name | command output | file:line>
- [C2 <short claim>] in-scope - fail - evidence: <what is missing or wrong>
- [C3 <short claim>] out-of-scope
- [C4 <short claim>] in-scope - unverifiable - evidence: <why it cannot be verified>

End with `**Unrequested changes**: <file:line - what was built that no in-scope
claim required, or (none)>`.
"""

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {
    "general-purpose": AgentType(
        slug="general-purpose",
        name="General-purpose agent",
        description="General-purpose agent for any sub-task.",
        tools=(
            "bash", "read_file", "search_replace", "multi_edit", "write_file",
            "delete_file", "multi_write", "grep", "list_dir", "web_fetch",
            "web_search", "lsp", "task",
        ),
        subagents_mode="inherit",
        max_turns=MAX_AGENT_TURNS,
        builtin=True,
    ),
    "explore": AgentType(
        slug="explore",
        name="Explore agent",
        description="Read-only agent for code exploration and research.",
        tools=_READONLY_TOOLS + ("task",),
        subagents_mode="list",
        subagents=("explore",),
        max_turns=DEFAULT_SUBAGENT_MAX_TURNS,
        builtin=True,
    ),
    "reviewer": AgentType(
        slug="reviewer",
        name="Spec reviewer",
        description=(
            "Review work that is already done against the requirement it claims "
            "to satisfy, and report per-claim evidence. It cannot edit or fix."
        ),
        tools=_REVIEW_TOOLS,
        instructions=_REVIEWER_INSTRUCTIONS,
        max_turns=DEFAULT_SUBAGENT_MAX_TURNS,
        builtin=True,
    ),
}


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def split_frontmatter(md: str) -> tuple[list[str], str]:
    """Return (frontmatter lines, body). Empty lines when there is no block."""
    if not md.startswith("---"):
        return [], md
    end = md.find("\n---", 3)
    if end == -1:
        return [], md
    return md[3:end].split("\n"), md[end + 4:].lstrip("\n")


def parse_fields(lines: Iterable[str]) -> dict[str, Any]:
    """Parse ``key: value`` / ``key: [a, b]`` / block ``- item`` lines.

    A deliberately tiny YAML subset: no nesting, no multi-line scalars, no
    quotes beyond a surrounding pair. Anything richer belongs in the body text.
    """
    out: dict[str, Any] = {}
    key: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(_unquote(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        key = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            out[key] = [_unquote(x.strip()) for x in v[1:-1].split(",") if x.strip()]
        elif v == "":
            out[key] = []
        else:
            out[key] = _unquote(v)
    return out


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


class SubagentRegistry:
    """Discover + validate subagent types for one working directory.

    Built-ins < global definitions < project definitions (same slug: last wins).
    One broken file never affects the others: it is dropped and reported through
    ``errors()`` (surfaced by the ``agents/list`` RPC).
    """

    def __init__(self, cwd: str, *, model_ids: set[str] | None = None):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
        self._model_ids = model_ids
        self._types: dict[str, AgentType] = {}
        self._errors: list[dict[str, str]] = []
        self._sig: tuple | None = None

    # --- discovery -----------------------------------------------------
    def _roots(self) -> list[tuple[Path, str]]:
        return [
            (Path.home() / ".cluxmate", "global"),
            (Path(self._cwd) / ".cluxmate", "project"),
        ]

    def _signature(self) -> tuple:
        sig: list[tuple[str, int, int]] = []
        for root, _source in self._roots():
            d = root / "agents"
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                sig.append((str(p), st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def _known_models(self) -> set[str] | None:
        if self._model_ids is not None:
            return self._model_ids
        from cluxmate.core.config import ConfigManager

        return {str(m.get("id", "")) for m in ConfigManager().list_models()}

    def _discover(self) -> None:
        types = dict(BUILTIN_AGENT_TYPES)
        errors: list[dict[str, str]] = []
        models = self._known_models()
        # Two passes, so a definition may reference ANY other slug regardless of
        # filename sort order or which root it lives in (spec §2.1 puts no
        # ordering precondition on `subagents: [slug, ...]`):
        #   pass 1 collects every valid-slug file from both roots (global first,
        #          project second — a later definition of the same slug wins);
        #   pass 2 builds each type and validates references against the
        #          built-ins PLUS all candidate slugs.
        # Per-file isolation (spec §2.3): one unreadable/invalid file lands in
        # `errors` and is dropped without affecting any other type.
        candidates: dict[str, tuple[dict[str, Any], str, Path, str]] = {}
        for root, source in self._roots():
            d = root / "agents"
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.md")):
                slug = path.stem
                if not _SLUG_RE.match(slug):
                    errors.append({"path": str(path), "error": (
                        f"invalid slug '{slug}': use [a-z0-9-], 1..64 chars"
                    )})
                    continue
                try:
                    text = path.read_text(encoding="utf-8")[:65536]
                except (OSError, ValueError) as e:
                    # UnicodeDecodeError is a ValueError subclass; one non-UTF-8
                    # file must not take the whole registry down with it.
                    errors.append({"path": str(path), "error": f"unreadable: {e}"})
                    continue
                lines, body = split_frontmatter(text)
                candidates[slug] = (parse_fields(lines), body, path, source)
        valid_slugs = set(types) | set(candidates)
        for slug, (fields, body, path, source) in candidates.items():
            try:
                atype, err = self._build_type(
                    slug, fields, body, source, path, models, valid_slugs
                )
            except Exception as e:  # defensive: one broken file never breaks others
                atype, err = None, f"unexpected error: {e}"
            if atype is None:
                err = err or "invalid definition"
                errors.append({"path": str(path), "error": err})
                print(f"[agents] dropped {path}: {err}", file=sys.stderr)
                continue
            types[slug] = atype
        self._types = types
        self._errors = errors

    def _build_type(self, slug, fields, body, source, path, models, slugs):
        description = str(fields.get("description", "")).strip()
        if not description:
            return None, "missing required 'description'"
        tools_raw = _as_list(fields.get("tools"))
        if tools_raw:
            bad = [t for t in tools_raw if t not in SUBAGENT_TOOL_NAMES]
            if bad:
                parent_only = [t for t in bad if t in PARENT_ONLY_TOOL_NAMES]
                if parent_only:
                    return None, (
                        f"tool(s) {', '.join(parent_only)} are parent-only and never "
                        f"available to a subagent"
                    )
                return None, f"unknown tool(s): {', '.join(bad)}"
            tools: tuple[str, ...] = tuple(tools_raw)
        else:
            tools = _READONLY_TOOLS
        model = str(fields.get("model", "inherit")).strip() or "inherit"
        if model != "inherit" and models is not None and model not in models:
            return None, f"unknown model id '{model}' (not in config.json)"
        max_turns_raw = fields.get("max_turns", DEFAULT_SUBAGENT_MAX_TURNS)
        try:
            max_turns = int(max_turns_raw)
        except (TypeError, ValueError):
            return None, f"max_turns must be an integer (got {max_turns_raw!r})"
        if max_turns <= 0:
            return None, f"max_turns must be positive (got {max_turns})"
        if max_turns > MAX_AGENT_TURNS:
            # spec §2.1: over the cap → clamp to 150 AND log a warning.
            max_turns = MAX_AGENT_TURNS
            print(
                f"[agents] {slug}: max_turns clamped to {MAX_AGENT_TURNS}",
                file=sys.stderr,
            )
        mode, subs = "none", ()
        subs_raw = _as_list(fields.get("subagents"))
        if len(subs_raw) == 1 and subs_raw[0] in ("none", "inherit"):
            mode = subs_raw[0]
        elif subs_raw:
            unknown = [s for s in subs_raw if s not in slugs]
            if unknown:
                return None, f"unknown subagent type(s): {', '.join(unknown)}"
            mode, subs = "list", tuple(subs_raw)
        return AgentType(
            slug=slug,
            name=str(fields.get("name", "")).strip() or slug,
            description=description,
            tools=tools,
            model=model,
            max_turns=max_turns,
            subagents_mode=mode,
            subagents=subs,
            instructions=body.strip(),
            builtin=False,
            source=source,
            path=str(path),
        ), None

    # --- public API ----------------------------------------------------
    def types(self) -> dict[str, AgentType]:
        sig = self._signature()
        if sig != self._sig:
            self._discover()
            self._sig = sig
        return dict(self._types)

    def slugs(self) -> list[str]:
        return list(self.types())

    def get(self, slug: str) -> AgentType | None:
        return self.types().get(slug)

    def errors(self) -> list[dict[str, str]]:
        self.types()  # ensure discovery ran
        return list(self._errors)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe catalog for the ``agents/list`` RPC / desktop card."""
        return {
            "agents": [
                {
                    "slug": t.slug,
                    "name": t.name,
                    "description": t.description,
                    "tools": list(t.tools),
                    "readonly": t.readonly,
                    "model": t.model,
                    "max_turns": t.max_turns,
                    "builtin": t.builtin,
                    "source": t.source,
                    "path": t.path,
                }
                for t in self.types().values()
            ],
            "errors": self.errors(),
        }
