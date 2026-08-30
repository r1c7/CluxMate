"""AgentBuilder — fluent API for constructing AgentLoop instances."""

import os
import platform
import shutil
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from cluxmate.core.providers.base import LLMProvider
from cluxmate.tools.base import BaseTool, ToolBridge
from cluxmate.tools.bash import BashTool, _bash_works, _is_wsl_bash
from cluxmate.tools._fence import ReadFence, WriteFence
from cluxmate.tools._sandbox import pick_sandbox, sandbox_disabled_by_env
from cluxmate.tools.read_file import ReadFileTool
from cluxmate.tools.search_replace import SearchReplaceTool
from cluxmate.tools.write_file import WriteFileTool
from cluxmate.tools.delete_file import DeleteFileTool
from cluxmate.tools.multi_edit import MultiEditTool
from cluxmate.tools.multi_write import MultiWriteTool
from cluxmate.tools.grep import GrepTool
from cluxmate.tools.list_dir import ListDirTool
from cluxmate.tools.task import TaskTool
from cluxmate.tools.skill import SkillTool
from cluxmate.tools.update_memory import UpdateMemoryTool
from cluxmate.tools.web_fetch import WebFetchTool
from cluxmate.tools.web_search import WebSearchTool
from cluxmate.tools.ask_user_question import AskUserQuestionTool
from cluxmate.core.skills import SkillManager
from cluxmate.core.memory import MemoryManager
from cluxmate.core.mcp import MCPManager
from cluxmate.core.lsp import LSPManager
from cluxmate.tools.lsp_tool import LspTool
from cluxmate.core.grants import GrantStore
from cluxmate.core.read_denies import ReadDenyStore
from cluxmate.core.hooks import HookManager
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.session_log_store import SessionLogStore
from cluxmate.templates.loader import render_system_prompt, render_child_prompt

from .agent import AgentLoop

# Maximum subagent recursion depth. Root is depth 0; a subagent may spawn its
# own subagents until this cap, at which point the `task` tool is withheld.
MAX_SUBAGENT_DEPTH = 4

# Subagent type definitions — each maps to a toolset and description
SUBAGENT_PROFILES: dict[str, dict[str, Any]] = {
    "general-purpose": {
        "description": "General-purpose agent for any sub-task.",
        "tools": ["bash", "read_file", "search_replace", "multi_edit", "write_file", "delete_file", "multi_write", "grep", "list_dir", "web_fetch", "web_search", "lsp"],
    },
    "explore": {
        "description": "Read-only agent for code exploration and research.",
        "tools": ["read_file", "grep", "list_dir", "web_fetch", "web_search", "lsp"],
    },
}


# Mode-specific instruction blocks. These were conditional sections of the
# system prompt; they are now injected as a synthetic ``source:"mode"`` user
# message so the stable system prompt stays mode-independent (and cache-stable)
# while a mode switch still tells the model its capabilities changed.
PLAN_MODE_BLOCK = (
    "<plan_mode>\n"
    "You are in **plan mode**. You have READ-ONLY tools only — you can read files, grep,\n"
    "and list directories, but you have NO ability to write, edit, delete, run shell\n"
    "commands, or delegate to subagents. This is a hard constraint enforced by the\n"
    "environment; those tools are not available to you and never will be in this mode.\n"
    "\n"
    "Your job here is to **investigate and plan, not to act**. Understand the request,\n"
    "explore the relevant code, and then respond with a clear, concrete plan: what\n"
    "changes you would make, in which files, and why. Present it for the user to review.\n"
    "\n"
    "When the request hinges on a user-owned decision or a fact you cannot discover\n"
    "by reading the code, use `ask_user_question` with concrete options rather than\n"
    "asking in prose. Do not ask where code lives or how current behavior works when\n"
    "you can find that out yourself.\n"
    "\n"
    "Do NOT attempt to make changes or look for workarounds to write (e.g. trying to\n"
    "find a shell, an alternate tool, or asking the user to run a write command for\n"
    "you as a substitute for doing it yourself). If the task requires edits, explain\n"
    "what you would do and tell the user to switch out of plan mode to carry it out.\n"
    "</plan_mode>"
)

AUTONOMOUS_MODE_BLOCK = (
    "<autonomous_mode>\n"
    "Every tool call you make — including destructive ones (deleting files, `rm`,\n"
    "overwriting, force operations) — executes IMMEDIATELY with no human confirmation.\n"
    "There is no approval step to catch a mistake. Act with the care that warrants:\n"
    "double-check paths and targets before destructive actions, prefer reversible\n"
    "steps, and don't delete or overwrite anything the task doesn't clearly require.\n"
    "</autonomous_mode>"
)


# Result of _detect_shell, cached for the process lifetime (module-level so it
# survives across builders). Detection spawns a bash subprocess to validate it —
# see _detect_shell docstring.
_SHELL_CACHE: tuple[str, str] | None = None


def _detect_shell() -> tuple[str, str]:
    """Return (shell_path, os_name) for the system prompt.

    On Windows, prefer a real bash if found on PATH (Git Bash, WSL sh, etc.)
    so the system prompt shell name matches what BashTool actually runs.
    Falls back to cmd.exe when no bash is available.

    Result is cached for the process lifetime — it's called on every build
    AND every refresh_system_prompt (per turn), and _bash_works spawns a
    subprocess (~100ms+ on Windows) each time.
    """
    global _SHELL_CACHE
    if _SHELL_CACHE is not None:
        return _SHELL_CACHE
    os_name = platform.system()
    if os_name == "Windows":
        bash = shutil.which("bash")
        # WSL's System32 bash.exe escapes the Low-IL sandbox (its Linux-side
        # DrvFS/interop bypass the Windows integrity boundary) and fails when
        # no distro is installed — never use it as the default shell, so the
        # prompt's shell name matches what BashTool actually runs.
        if bash and _bash_works(bash) and not _is_wsl_bash(bash):
            _SHELL_CACHE = (bash, os_name)
            return _SHELL_CACHE
        _SHELL_CACHE = ("cmd.exe", os_name)
        return _SHELL_CACHE
    shell = os.environ.get("SHELL", "/bin/bash")
    _SHELL_CACHE = (shell, os_name)
    return _SHELL_CACHE


class AgentBuilder:
    """Fluent builder for AgentLoop instances."""

    def __init__(self, cwd: str, provider: LLMProvider):
        self._cwd = cwd
        self._provider = provider
        self._model = "claude-sonnet-4-6"
        # Whether the active model supports a 1M context window. Drives the
        # agent's compaction budget (1M vs 128K) and is inherited by children.
        self._context_1m = False
        self._tools: list[BaseTool] = []
        self._include_default_tools = False
        self._subagent_types: list[str] = []
        self._custom_prompt: str | None = None
        self._subagent_type: str | None = None
        self._task_description: str = ""
        # Recursion + tracking state. Root builder is depth 0 with agent_id
        # "root"; build_child produces deeper builders that can recurse.
        self._depth = 0
        self._agent_id = "root"
        # Development mode (plan/default/acceptEdits/yolo). Only "plan" changes
        # the toolset — it hard-isolates to read-only tools so writes can't be
        # issued at all. The other modes affect approval, not the tools present.
        self._mode = "default"
        # Per-turn tracker (an AgentCallbacks-like object exposing
        # on_agent_start/on_agent_end/scoped). Set by set_tracker each turn.
        self._tracker: Any = None
        # MCP client manager — constructed lazily on the first _get_tools
        # call (depth 0 only) and cached. NOT per-turn: spawning subprocesses
        # every turn would be broken. Children inherit None and the depth
        # gate keeps them from ever constructing their own.
        self._mcp: MCPManager | None = None
        # LSP manager — constructed lazily on first _get_tools (depth 0 only)
        # and shared with subagents so children never re-spawn a language
        # server for the same workspace.
        self._lsp: LSPManager | None = None
        # When True, _get_tools constructs the MCP manager but does NOT load it
        # (load spawns npx/subprocesses and blocks). The caller drives load()
        # itself — the jsonrpc server does this in a background thread so the
        # initialize handshake returns fast. See with_deferred_mcp / load_mcp.
        self._defer_mcp = False
        # Serializes MCP load vs shutdown: deferred load runs on a background
        # thread while a re-initialize may shut this builder down concurrently.
        # _mcp_closed latches on shutdown so a racing load_mcp bails instead of
        # re-spawning subprocesses the shutdown just reclaimed.
        self._mcp_lock = threading.Lock()
        self._mcp_closed = False
        # Fingerprint of the last injected memory/skills — injection is idempotent
        # across turns so identical context isn't re-appended every turn.
        self._last_injection_sig: tuple | None = None
        # Last mode whose instruction block was injected. None until the first
        # turn, so a non-default starting mode is injected on turn 1.
        self._last_mode: str | None = None
        # Subagent logging: build() stamps this agent's session log/id; build_child
        # mints a child SessionLog (origin="subagent", parentSession=<this id>) and
        # persists it through _log_store. _session_log/_session_id are THIS agent's
        # own (never inherited by children); _log_store IS inherited so grandchildren
        # persist too.
        self._session_log: SessionLog | None = None
        self._session_id: str | None = None
        self._log_store: SessionLogStore | None = None
        # Writable-folder grants (sandbox-grants.json). Shared across rebuilds
        # and inherited by children; None → no extra granted folders.
        self._grants: GrantStore | None = None
        # Read-denylist (forbid-read.json). Shared across rebuilds and inherited
        # by children; None → empty deny set (no read restrictions).
        self._read_denies: ReadDenyStore | None = None
        # SSRF network-access config (~/.cluxmate/ssrf.json). Shared across
        # rebuilds and inherited by children; None → default deny (no allow).
        self._ssrf: SsrConfig | None = None
        # Lifecycle hooks (settings.json). Lazy: constructed on first build when
        # the caller didn't inject one, then cached. Inherited by children so
        # subagent tool calls are hooked too.
        self._hooks: HookManager | None = None

    def with_default_tools(self) -> "AgentBuilder":
        self._include_default_tools = True
        return self

    def with_grants(self, store: "GrantStore | None") -> "AgentBuilder":
        """Attach the writable-folder grant registry (shared across rebuilds)."""
        self._grants = store
        return self

    def with_read_denies(self, store: "ReadDenyStore | None") -> "AgentBuilder":
        """Attach the read-denylist registry (shared across rebuilds)."""
        self._read_denies = store
        return self

    def with_ssrf(self, config: "SsrConfig | None") -> "AgentBuilder":
        """Attach the SSRF network-access config (shared across rebuilds and
        inherited by children)."""
        self._ssrf = config
        return self

    def with_hooks(self, hooks: "HookManager | None") -> "AgentBuilder":
        """Attach a HookManager (shared across rebuilds and inherited by children).

        The JSON-RPC server injects one so it can stamp the session id; the
        headless CLI/TUI leave it unset and build() constructs one lazily.
        """
        self._hooks = hooks
        return self

    def _hooks_manager(self) -> "HookManager | None":
        """Current HookManager, lazily constructed from the cwd when unset."""
        if self._hooks is None:
            self._hooks = HookManager(self._cwd)
        return self._hooks

    def reload_hooks(self) -> list[dict[str, Any]]:
        """Re-read settings.json in place (no session restart) and return the
        new normalized hook list. The live agent keeps its reference to the SAME
        HookManager, so the refreshed specs apply from the next hook event."""
        hooks = self._hooks_manager()
        if hooks is None:
            return []
        hooks.reload()
        return hooks.list_hooks()

    def _grant_paths(self) -> list[str]:
        if self._grants is None:
            return []
        return self._grants.snapshot()

    def _forbid_read_paths(self) -> list[str]:
        if self._read_denies is None:
            return []
        return self._read_denies.snapshot()

    def with_model(self, name: str) -> "AgentBuilder":
        self._model = name
        return self

    def with_provider(self, provider: LLMProvider) -> "AgentBuilder":
        """Swap the LLM provider in place (mid-session model switch).

        The builder caches MCP, grants, mode and the tracker, so a rebuild after
        this swap re-renders the prompt/tool schemas with the new provider but
        does NOT re-spawn MCP subprocesses. Children inherit the new provider via
        ``_child_builder``, which reads ``self._provider`` at build time.
        """
        self._provider = provider
        return self

    def with_context_1m(self, supported: bool) -> "AgentBuilder":
        self._context_1m = supported
        return self

    def _context_window(self) -> int:
        return 1_000_000 if self._context_1m else 128_000

    def with_subagent_types(self, types: list[str]) -> "AgentBuilder":
        self._subagent_types = types
        return self

    def with_agent_id(self, agent_id: str) -> "AgentBuilder":
        self._agent_id = agent_id
        return self

    def with_mode(self, mode: str) -> "AgentBuilder":
        """Set the development mode. Only 'plan' changes the toolset (read-only
        hard isolation); other modes leave the tools present and affect approval
        via PermissionPolicy instead."""
        self._mode = mode
        return self

    def set_tracker(self, tracker: Any) -> "AgentBuilder":
        """Attach a per-turn tracker used to emit subagent lifecycle events.

        Called each turn (trackers are per-chat/send while the builder is
        constructed once at initialize). Passing None disables tracking.
        """
        self._tracker = tracker
        return self

    def with_tool(self, tool: BaseTool) -> "AgentBuilder":
        self._tools.append(tool)
        return self

    def with_mcp(self, mcp: "MCPManager | None") -> "AgentBuilder":
        """Inject a pre-built (already loaded) MCP manager.

        Lets callers share one manager across builders/agents per working
        directory — MCPManager.load() spawns subprocesses and runs the
        tools/list handshake, so rebuilding an agent must not re-spawn them.
        """
        self._mcp = mcp
        return self

    def with_log_store(self, store: "SessionLogStore | None") -> "AgentBuilder":
        """Inject the session-log store so subagents can persist their own logs.

        The parent builder carries this store; build_child mints a child
        SessionLog and writes it (header + events) through the store. None (the
        default, e.g. headless CLI) keeps subagents unlogged.
        """
        self._log_store = store
        return self

    def attach_session_log(self, session_log: "SessionLog | None") -> "AgentBuilder":
        """Refresh THIS builder's live session log/id.

        ``build()`` stamps these; this lighter setter re-stamps them without a
        rebuild. Callers re-point it after undo/reload replaces the live log
        object, so ``build_child`` records ``subagent/spawn`` events (and reads
        the correct turn) against the ACTIVE log, never a stale one.

        On the FIRST attach to a non-empty log in this process (the fingerprints
        are still unset — i.e. fresh after a crash/reopen), rebuild the injection
        fingerprints from the log so unchanged memory/skills/mode are not
        re-injected on the first turn of the new process.
        """
        self._session_log = session_log
        self._session_id = session_log.id if session_log is not None else None
        if (
            self._session_log is not None
            and self._last_injection_sig is None
            and self._last_mode is None
        ):
            self._rebuild_injections_from_log()
        return self

    def with_custom_prompt(self, prompt: str) -> "AgentBuilder":
        self._custom_prompt = prompt
        return self

    def with_deferred_mcp(self) -> "AgentBuilder":
        """Skip the blocking MCP load() during build().

        MCPManager.load() spawns subprocesses (npx) and runs the tools/list
        handshake — seconds of latency the caller may not want on the critical
        path (the jsonrpc initialize handshake). With this set, _get_tools still
        constructs the manager but leaves it unloaded, so build() returns without
        MCP tools. The caller then calls load_mcp() (e.g. on a background thread)
        and rebuilds the agent once tools are ready. Parent (depth 0) only.
        """
        self._defer_mcp = True
        return self

    def load_mcp(self) -> bool:
        """Load the (deferred) MCP manager. Returns True if any tools appeared.

        Idempotent-ish: constructs the manager if _get_tools hasn't yet, then
        runs the blocking load(). MCPManager.load() is itself idempotent (guards
        on self._loaded), so a redundant call is a no-op. Safe to call from a
        background thread — it only mutates self._mcp, which build() reads by
        reference (the agent swap happens in the caller).
        """
        if self._depth != 0 or not self._include_default_tools:
            return False
        with self._mcp_lock:
            # A concurrent mcp_shutdown (re-initialize superseded us) latched
            # closed — don't re-spawn what it just reclaimed.
            if self._mcp_closed:
                return False
            if self._mcp is None:
                self._mcp = MCPManager(self._cwd, sandbox=self._mcp_sandbox())
            mcp = self._mcp
        # load() spawns subprocesses and blocks — run it OUTSIDE the lock so a
        # concurrent mcp_shutdown isn't held off for the whole handshake. load()
        # is idempotent and thread-safe against its own shutdown.
        mcp.load()
        with self._mcp_lock:
            # If shutdown fired during load(), the manager was torn down (and
            # possibly cleared); report no tools so the caller doesn't swap in a
            # dead agent.
            if self._mcp_closed or self._mcp is None:
                return False
            return bool(self._mcp.list_tools())

    def _get_tools(self) -> list[BaseTool]:
        tools: list[BaseTool] = list(self._tools)
        if self._include_default_tools:
            # Read denylist fence (read_file/grep/list_dir). Default empty → a
            # no-op ReadFence; only paths in ~/.cluxmate/forbid-read.json block.
            read_fence = ReadFence(deny_paths=self._forbid_read_paths())
            # Plan mode: hard isolation. Register only read-only tools so the
            # model literally cannot issue a write — no bash (can mutate files),
            # no edit/write/delete, no task (a subagent could write and bypass
            # this), no update_memory, no MCP. SkillTool stays (read-only load).
            # Reuse the explore subagent's read-only set as the single source of
            # truth for "what counts as read-only".
            if self._mode == "plan":
                readonly = set(SUBAGENT_PROFILES["explore"]["tools"]) | {"web_fetch", "web_search"}
                tools.extend([
                    t for t in (
                        ReadFileTool(workdir=self._cwd, fence=read_fence),
                        GrepTool(workdir=self._cwd, fence=read_fence),
                        ListDirTool(workdir=self._cwd, fence=read_fence),
                        WebFetchTool(plan_mode=True, ssrf=self._ssrf),
                        WebSearchTool(ssrf=self._ssrf),
                        LspTool(manager=self._lsp_manager()),
                    ) if t.name in readonly
                ])
                if self._depth == 0 and SkillManager(self._cwd).discover_enabled():
                    tools.append(SkillTool(cwd=self._cwd, builder=self))
                # ask_user_question is read-only, so it stays available in plan
                # mode — clarifying questions are how plan mode disambiguates a
                # spec without writing anything.
                if self._depth == 0:
                    tools.append(AskUserQuestionTool(builder=self))
                return tools
            # Write fence (sandbox phase 0): file write/delete tools may only
            # touch the workspace + temp dir + granted folders. Disabled in
            # yolo mode (the explicit opt-out). Mode is baked in per build;
            # chat/set_mode rebuilds, so a switch re-arms/disarms with the
            # new toolset.
            fence = WriteFence(
                self._cwd,
                enabled=self._mode != "yolo",
                grant_paths=self._grant_paths(),
            )
            # Shell sandbox (phase 1): bash runs under an OS sandbox backend
            # (bwrap / low-IL). Same rule as the fence: off in yolo. When no
            # backend is available, sandbox_required=True makes BashTool
            # FAIL CLOSED (refuse) instead of running bare — unless the user
            # explicitly opted out via CLUXMATE_BASH_SANDBOX=off.
            sandbox_enabled = self._mode != "yolo" and not sandbox_disabled_by_env()
            sandbox = (
                pick_sandbox(
                    self._cwd,
                    grant_paths=self._grant_paths(),
                    deny_read_paths=self._forbid_read_paths(),
                )
                if sandbox_enabled else None
            )
            tools.extend([
                BashTool(
                    workdir=self._cwd,
                    sandbox=sandbox,
                    sandbox_required=sandbox_enabled,
                ),
                ReadFileTool(workdir=self._cwd, fence=read_fence),
                SearchReplaceTool(workdir=self._cwd, fence=fence),
                WriteFileTool(workdir=self._cwd, fence=fence),
                DeleteFileTool(workdir=self._cwd, fence=fence),
                MultiEditTool(workdir=self._cwd, fence=fence),
                MultiWriteTool(workdir=self._cwd, fence=fence),
                GrepTool(workdir=self._cwd, fence=read_fence),
                ListDirTool(workdir=self._cwd, fence=read_fence),
                WebFetchTool(ssrf=self._ssrf),
                WebSearchTool(ssrf=self._ssrf),
                LspTool(manager=self._lsp_manager()),
            ])
            # Add TaskTool only when subagent types are configured AND we have
            # not hit the recursion cap. Withholding `task` at the cap is what
            # stops runaway subagent nesting.
            if self._subagent_types and self._depth < MAX_SUBAGENT_DEPTH:
                tools.append(TaskTool(builder=self))
            # use_skill only for the parent (depth 0) and only when enabled skills exist.
            # Subagents don't get skills this round (avoids scope creep).
            if self._depth == 0 and SkillManager(self._cwd).discover_enabled():
                tools.append(SkillTool(cwd=self._cwd, builder=self))
            # update_memory only for the parent (depth 0) — subagents shouldn't
            # write durable memory (explore/subtasks would pollute it), matching
            # the SkillTool/MCP parent-only gate.
            if self._depth == 0:
                tools.append(UpdateMemoryTool(cwd=self._cwd))
            # Parent-only: a subagent must not block the parent's `task` call
            # waiting on a human answer (mirrors DSH's DELEGATED_CALLER rule).
            if self._depth == 0:
                tools.append(AskUserQuestionTool(builder=self))
            # MCP tools only for the parent (depth 0). Construct the manager
            # on first access and cache it — load() spawns subprocesses and
            # runs the tools/list handshake, so it must NOT happen per turn
            # (refresh_system_prompt calls _get_tools each turn).
            if self._depth == 0:
                if self._mcp is None:
                    self._mcp = MCPManager(self._cwd, sandbox=self._mcp_sandbox())
                    # Deferred mode: construct but don't load here (load spawns
                    # subprocesses and blocks). The caller loads it off the
                    # critical path and rebuilds. list_tools() is empty until
                    # then, so this build simply omits MCP tools.
                    if not self._defer_mcp:
                        self._mcp.load()
                tools.extend(self._mcp.list_tools())
        return tools

    def _shell_sandbox(self):
        """Backend for bash/MCP when sandboxing is on, else None.

        Same rule for both: off in yolo, off on explicit env opt-out. Bash is
        FAIL-CLOSED when the backend is missing (model-generated commands);
        MCP is best-effort (user-configured servers) — the caller decides.
        """
        if self._mode == "yolo" or sandbox_disabled_by_env():
            return None
        return pick_sandbox(
            self._cwd,
            grant_paths=self._grant_paths(),
            deny_read_paths=self._forbid_read_paths(),
        )

    def _mcp_sandbox(self):
        """Best-effort sandbox for MCP stdio servers (None → bare Popen)."""
        return self._shell_sandbox()

    def mcp_status(self) -> list[dict[str, Any]]:
        """Live status of all configured MCP servers. Empty if not loaded."""
        if self._mcp is None:
            return []
        return self._mcp.status()

    def mcp_shutdown(self) -> None:
        """Kill MCP subprocesses / close HTTP clients. Idempotent.

        Latches _mcp_closed so a concurrent deferred load_mcp (background thread)
        bails instead of re-spawning. If load() is mid-handshake, MCPManager.load
        is itself thread-safe against shutdown — the spawned clients get killed
        and load_mcp's post-lock check reports no tools.
        """
        with self._mcp_lock:
            self._mcp_closed = True
            mcp = self._mcp
            self._mcp = None
        if mcp is not None:
            mcp.shutdown()

    def _lsp_manager(self) -> "LSPManager":
        """Lazy, cached LSP manager for this builder's cwd. Shared by children."""
        if self._lsp is None:
            self._lsp = LSPManager(self._cwd, sandbox=self._shell_sandbox())
        return self._lsp

    def lsp_shutdown(self) -> None:
        """Kill lazily-spawned language servers. Idempotent."""
        if self._lsp is not None:
            self._lsp.shutdown()
            self._lsp = None

    def _render_system_prompt(self, tools: list[BaseTool]) -> str:
        """Render the STABLE system prompt.

        Mode instructions, tool prose, memory and skills are deliberately NOT
        rendered here: mode is a ``source:"mode"`` injection (see
        :meth:`injections_for_turn`), tools are passed as schemas via the API
        (the authoritative source), and memory/skills are ``source:"memory"`` /
        ``source:"skill"`` injections. Keeping them out makes the request prefix
        stable so a mode/memory/tool change doesn't invalidate the whole cache.
        """
        if self._custom_prompt is not None:
            return self._custom_prompt
        shell_path, os_name = _detect_shell()
        has_update_memory = any(
            getattr(t, "name", "") == "update_memory" for t in tools
        )
        return render_system_prompt(
            os_name=os_name,
            shell_path=shell_path,
            working_directory=self._cwd,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            has_update_memory=has_update_memory,
        )

    def render_injections(self) -> list[tuple[str, str]]:
        """Current ``(source, content)`` synthetic user messages: memory + skills.

        ``source`` is ``"memory"`` or ``"skill"`` — recorded on the logged
        ``user/message`` event so the UI can fold them and replay can tell them
        apart from human input.
        """
        parts: list[tuple[str, str]] = []
        project_memory = MemoryManager(self._cwd).render()
        if project_memory:
            parts.append(("memory", (
                "[Project memory]\n"
                "Durable context the user maintains for this environment. Treat it as\n"
                "authoritative background — follow its conventions unless the current\n"
                "request overrides them.\n\n" + project_memory
            )))
        skills = SkillManager(self._cwd).discover_enabled()
        if skills:
            skills_list = "\n".join(
                f"- **{s.slug}**: {s.description or s.name}" for s in skills
            )
            parts.append(("skill", (
                "[Available skills]\n"
                "Skills are reusable instruction sets identified by a slug. When a skill\n"
                "is relevant, call the `use_skill` tool with its slug.\n\n" + skills_list
            )))
        return parts

    def _render_mode_block(self) -> str | None:
        """Mode-specific instruction text for the current mode, or None when the
        mode is the default baseline (already described by the stable prompt)."""
        if self._mode == "plan":
            return PLAN_MODE_BLOCK
        if self._mode == "yolo":
            return AUTONOMOUS_MODE_BLOCK
        return None

    def _mode_message_for_turn(self) -> str | None:
        """Mode injection for THIS turn, or None when the mode didn't change.

        A mode switch changes the model's capabilities, so it must be told
        explicitly: the first turn injects the current mode block when it is
        non-default; a mid-session change injects a ``[Mode changed X→Y]`` note
        plus the new block.
        """
        if self._mode == self._last_mode:
            return None
        prev = self._last_mode
        self._last_mode = self._mode
        block = self._render_mode_block()
        if prev is None:
            return block  # first turn: None for default, block for plan/yolo
        if self._mode == "default":
            return (
                f"[Mode changed {prev} → default — the full toolset and approval "
                f"policy are restored.]"
            )
        return f"[Mode changed {prev} → {self._mode}]\n{block}"

    def injections_for_turn(self) -> list[tuple[str, str]]:
        """Injections for THIS turn: memory/skills when they changed since the
        last turn (or on the first turn), plus a mode message when it changed."""
        injections: list[tuple[str, str]] = []
        mem_skill = self.render_injections()
        sig = tuple(mem_skill)
        if sig != self._last_injection_sig:
            injections.extend(mem_skill)
            self._last_injection_sig = sig
        mode_msg = self._mode_message_for_turn()
        if mode_msg is not None:
            injections.append(("mode", mode_msg))
        return injections

    def invalidate_injections(self) -> None:
        """Drop the injection fingerprints so the next turn re-injects the current
        memory/skills and mode. Call after a turn whose compaction may have folded
        earlier injections into the summary."""
        self._last_injection_sig = None
        self._last_mode = None

    def _rebuild_injections_from_log(self) -> None:
        """Reconstruct injection fingerprints from the session log (single source
        of truth).

        Called once per process, when the builder first attaches to an EXISTING
        log with the fingerprints still unset — i.e. crash recovery / session
        reopen. Memory/skills are suppressed only when the log's last injection
        of each source matches the current render and was not followed by a
        compaction (which may have folded it). Mode is trusted only when a mode
        announcement exists after the last compaction.
        """
        log = self._session_log
        if log is None or log.seq == 0:
            return
        last_mem: str | None = None
        last_mem_seq: int | None = None
        last_skill: str | None = None
        last_skill_seq: int | None = None
        last_mode_seq: int = -1
        last_compaction_seq: int = -1
        last_header_mode: str | None = None
        for event in log.events:
            if event.type == "request/header":
                last_header_mode = (
                    event.data.get("header", {}).get("config", {}).get("mode")
                )
            elif event.type == "user/message":
                src = event.data.get("source")
                content = (event.data.get("message") or {}).get("content")
                if not isinstance(content, str):
                    continue
                if src == "memory":
                    last_mem, last_mem_seq = content, event.seq
                elif src == "skill":
                    last_skill, last_skill_seq = content, event.seq
                elif src == "mode":
                    last_mode_seq = event.seq
                elif src == "compaction":
                    last_compaction_seq = event.seq

        current = self.render_injections()
        if current:
            matches = True
            for src, content in current:
                if src == "memory":
                    seen, seq = last_mem, last_mem_seq
                else:
                    seen, seq = last_skill, last_skill_seq
                if seen != content or seq is None or seq < last_compaction_seq:
                    matches = False
                    break
            if matches:
                self._last_injection_sig = tuple(current)

        if last_header_mode is not None and last_mode_seq > last_compaction_seq:
            self._last_mode = last_header_mode

    def refresh_system_prompt(self, agent: "AgentLoop") -> None:
        """Re-render the system prompt in place on an existing agent.

        Called at the start of each turn so environment facts (shell, working
        directory, date) stay current. Memory and skills are NOT part of the
        system prompt — they are re-read each turn as synthetic user messages
        via :meth:`injections_for_turn`.
        """
        agent.system_prompt = self._render_system_prompt(self._get_tools())

    def build(self, session_log: SessionLog | None = None) -> "AgentLoop":
        """Build the parent agent. ``session_log``, when given, is attached so
        every run() emits session events; rebuild points (mode switch, MCP-ready,
        re-initialize) must pass the same log or recording stops."""
        self.attach_session_log(session_log)
        tools = self._get_tools()
        bridge = ToolBridge()
        for t in tools:
            bridge.register(t)

        system_prompt = self._render_system_prompt(tools)

        return AgentLoop(
            model=self._model,
            provider=self._provider,
            tools=bridge,
            system_prompt=system_prompt,
            context_window=self._context_window(),
            session_log=session_log,
            mode=self._mode,
            hooks=self._hooks_manager(),
        )

    def _child_builder(self, subagent_type: str, agent_id: str) -> "AgentBuilder":
        """Construct the deeper AgentBuilder backing a subagent.

        The child carries depth+1, the same provider/model/cwd/tracker, and its
        own agent_id. `general-purpose` children keep the subagent types so they
        can recurse (still depth-gated in _get_tools); `explore` children recurse
        only within their own type (`["explore"]`), keeping the chain read-only.
        """
        child = AgentBuilder(self._cwd, self._provider)
        child._model = self._model
        child._context_1m = self._context_1m
        child._include_default_tools = self._include_default_tools
        child._depth = self._depth + 1
        child._agent_id = agent_id
        child._tracker = self._tracker
        child._log_store = self._log_store
        child._mode = self._mode
        child._grants = self._grants
        child._read_denies = self._read_denies
        child._ssrf = self._ssrf
        child._hooks = self._hooks
        child._lsp = self._lsp
        child._subagent_type = subagent_type
        if subagent_type == "general-purpose":
            child._subagent_types = list(self._subagent_types)
        else:
            # explore recurses within its own type only: it gains the `task`
            # tool (depth-gated), but the TaskTool allowlist forbids spawning
            # general-purpose grandchildren, which would leak write access
            # past the read-only gate.
            child._subagent_types = ["explore"]
        return child

    def build_child(
        self, subagent_type: str, task_description: str, agent_id: str = ""
    ) -> "AgentLoop":
        """Build a subagent AgentLoop with restricted tools and child prompt.

        Mints a child SessionLog (origin="subagent", parentSession=<this agent's
        session id>) and attaches it, so the subagent's turns are recorded and
        persisted to its own <child_id>.jsonl (see _make_child_log).
        """
        profile = SUBAGENT_PROFILES.get(subagent_type, SUBAGENT_PROFILES["general-purpose"])
        allowed_names = profile["tools"]

        child_id = agent_id or uuid.uuid4().hex
        child = self._child_builder(subagent_type, child_id)
        # Subagent logging: the child inherits the store but gets its OWN log/id
        # (never the parent's), so a grandchild links to THIS child, not the root.
        child._session_log = self._make_child_log(subagent_type, child_id, task_description)
        child._session_id = child_id if child._session_log is not None else None
        # The child's own toolset (may include a depth-gated `task` so both
        # subagent types can recurse — general-purpose to either type,
        # explore only to explore, enforced by TaskTool's allowlist).
        tools = child._get_tools()
        # Keep only tools this profile permits; `task` is allowed through so
        # children can recurse (general-purpose to either type, explore only
        # to explore — the allowlist in TaskTool.execute is the gate).
        child_tools = [t for t in tools if t.name in allowed_names or t.name == "task"]

        bridge = ToolBridge()
        for t in child_tools:
            bridge.register(t)

        tools_list = self._format_tools_list(child_tools)
        shell_path, os_name = _detect_shell()
        system_prompt = render_child_prompt(
            subagent_type=subagent_type,
            task_description=task_description,
            tools_list=tools_list,
            os_name=os_name,
            shell_path=shell_path,
            working_directory=self._cwd,
        )

        return AgentLoop(
            model=self._model,
            provider=self._provider,
            tools=bridge,
            system_prompt=system_prompt,
            context_window=self._context_window(),
            session_log=child._session_log,
            mode=self._mode,
            hooks=child._hooks_manager(),
        )

    def _make_child_log(
        self, subagent_type: str, child_id: str, task_description: str
    ) -> "SessionLog | None":
        """Create (and persist the header of) a subagent's own SessionLog.

        Returns None when no store is configured (headless/REPL) — the child then
        runs unlogged, exactly as before. With a store, this also appends a
        ``subagent/spawn`` event to THIS agent's log (the parent → child pointer
        for replay); the child header's ``parentSession`` is the reverse child →
        parent pointer. ``agent_id == session_id == child_id``.
        """
        if self._log_store is None:
            return None
        parent = self._session_log.header if self._session_log is not None else None
        header = SessionHeader(
            id=child_id,
            createdAt=int(time.time() * 1000),
            cwd=(parent.cwd if parent is not None else None) or self._cwd or None,
            provider=parent.provider if parent is not None else "",
            model=parent.model if parent is not None else self._model,
            apiType=parent.apiType if parent is not None else "openai",
            origin="subagent",
            parentSession=self._session_id,
            delegationDepth=self._depth + 1,
        )
        self._log_store.create(header)
        if self._session_log is not None:
            self._session_log.append(
                "subagent/spawn",
                {
                    "session_id": child_id,
                    "parent_session": self._session_id,
                    "agent_id": child_id,
                    "parent_id": self._agent_id,
                    "subagent_type": subagent_type,
                    "description": task_description,
                    "depth": self._depth + 1,
                    "turn": self._session_log.turn_count,
                },
            )
        return SessionLog.create(header)

    def _format_tools_list(self, tools: list[BaseTool]) -> str:
        lines = []
        for t in tools:
            lines.append(f"- **{t.name}**: {t.description}")
        return "\n".join(lines)
