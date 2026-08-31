"""Glue between TUI and AgentLoop — creates and runs agent turns."""

import threading

from cluxmate.core.agent import AgentCallbacks, AgentLoop, AgentResult, ToolDecision
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.config import ConfigManager
from cluxmate.core.reasoning import default_for, options_for
from cluxmate.core.mcp import MCPManager
from cluxmate.core.permissions import PermissionPolicy
from cluxmate.core.session_log import SessionLog
from cluxmate.core.session_log_store import IncrementalPersister
from cluxmate.core.session_store import SessionStore
from cluxmate.core.providers.base import LLMProvider


def _create_provider(entry: dict) -> LLMProvider:
    from cluxmate.core.providers.factory import build_provider
    return build_provider(entry)


class TuiController:
    """Orchestrates agent lifecycle for the TUI."""

    def __init__(self):
        self.config = ConfigManager()
        self.sessions = SessionStore()
        self._active_session_id: str | None = None
        self._agent: AgentLoop | None = None
        self._builder: AgentBuilder | None = None
        self._model_id: str = ""
        # Per-session reasoning level (an effort id, or None = provider default).
        # Reset to provider default on a model switch; kept across same-model
        # rebuilds (MCP ready / mode change).
        self._reasoning_effort: str | None = None
        # Build key — (cwd, model_name, mode, model_id). Rebuilding the agent
        # is expensive (MCPManager.load spawns subprocesses, system prompt
        # render reads skills/memory), so skip it when nothing changed.
        self._build_key: tuple | None = None
        # One MCPManager per working directory, shared across builders/agents.
        # A fresh manager per build would re-spawn MCP subprocesses on every
        # session switch. Managers are loaded lazily on first use per cwd.
        self._mcp_cache: dict[str, "MCPManager"] = {}
        # Background load threads per cwd — MCPManager.load() spawns subprocesses
        # (e.g. npx) + runs the tools/list handshake, which can take seconds.
        # Loading off the UI thread keeps session switching responsive; the
        # agent is rebuilt once the load completes (see _maybe_rebuild_after_mcp).
        self._mcp_threads: dict[str, threading.Thread | None] = {}
        # Approval policy for the current working directory. Created when the
        # agent is built; mode is synced by set_mode. Without it, mode changes
        # (acceptEdits/yolo/default) would have no effect on tool approval.
        self._policy: PermissionPolicy | None = None
        # Live event log for the active session + its incremental persister, which
        # writes each appended event to JSONL immediately so a crash mid-turn
        # leaves the partial turn on disk (load() repairs the open turn on restart).
        self._session_log: SessionLog | None = None
        self._persister: IncrementalPersister | None = None

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    # ── session management ─────────────────────────────────

    def new_session(self, model_id: str, cwd: str, mode: str = "default") -> str:
        entry = self.config.get_model(model_id) or {}
        sid = self.sessions.create(
            "", entry.get("provider", ""), entry.get("model_name", ""), cwd,
            model_id=model_id, api_type=entry.get("api_type", ""),
            reasoning_effort=default_for(entry),
        )
        self._active_session_id = sid
        self._session_log = self.sessions.load_log(sid)
        self._bind_persister()
        self._build_agent(model_id, cwd, mode)
        return sid

    def switch_session(
        self, session_id: str, mode: str = "default", fallback_cwd: str = ""
    ) -> dict | None:
        data = self.sessions.load(session_id)
        if data is None:
            return None
        self._active_session_id = session_id
        self._session_log = self.sessions.load_log(session_id)
        self._bind_persister()
        # Desktop-format sessions carry no working_dir (the desktop keeps it in
        # SQLite) — fall back to the current TUI cwd so the agent isn't built
        # with an empty working directory.
        self._build_agent(
            data.get("model_id", ""), data.get("working_dir") or fallback_cwd, mode,
        )
        # Restore the session's persisted reasoning level (the build just reset it
        # to the model's default). "default" (don't send) is always valid; a raw
        # value must still be in the model's list.
        saved = data.get("reasoning_effort")
        if saved is not None:
            entry = self.config.get_model(self._model_id) or {}
            if saved in options_for(entry):
                self.set_reasoning_effort(saved)
        return data

    def delete_session(self, session_id: str):
        self.sessions.delete(session_id)
        if self._active_session_id == session_id:
            self._active_session_id = None
            self._session_log = None
            self._dispose_persister()
            self._agent = None
            self._builder = None
            self._model_id = ""
            self._reasoning_effort = None
            self._build_key = None
            self._policy = None

    def _bind_persister(self) -> None:
        """(Re)bind incremental JSONL persistence to the active session log."""
        self._dispose_persister()
        if self._active_session_id and self._session_log is not None:
            self._persister = IncrementalPersister(
                self.sessions.log_store, self._active_session_id, self._session_log
            )

    def _dispose_persister(self) -> None:
        if self._persister is not None:
            self._persister.dispose()
            self._persister = None

    def list_sessions(self) -> list[dict]:
        return self.sessions.list_all()

    def get_session_data(self) -> dict | None:
        if self._active_session_id:
            return self.sessions.load(self._active_session_id)
        return None

    def set_mode(self, mode: str):
        # Sync the approval policy first so mode affects tool approval, not
        # just the toolset. "plan" also changes the toolset (read-only) below.
        if self._policy is not None:
            self._policy.set_mode(mode)
        if self._builder is not None:
            self._builder.with_mode(mode)
            self._agent = self._builder.build(session_log=self._session_log)
            # Keep the build key in sync so a later _build_agent with the same
            # (cwd, model, mode) doesn't rebuild what set_mode just rebuilt.
            if self._build_key is not None:
                self._build_key = (
                    self._build_key[0], self._build_key[1], mode, self._build_key[3],
                )

    def set_model(self, model_id: str):
        """Switch the active session's model (rebuilds the agent).

        Resets the reasoning level to the new model's preset default and persists
        both the model and the level. No-op if the model didn't change or no
        agent is built yet.
        """
        if self._agent is None or model_id == self._model_id:
            return
        entry = self.config.get_model(model_id) or {}
        cwd = self._build_key[0] if self._build_key else ""
        mode = self._build_key[2] if self._build_key else "default"
        self._build_agent(model_id, cwd, mode)
        if self._active_session_id:
            self.sessions.set_model(
                self._active_session_id, model_id,
                entry.get("provider", ""), entry.get("model_name", ""),
                entry.get("api_type", ""),
            )
            # _build_agent reset _reasoning_effort to the new default; persist it.
            self.sessions.set_reasoning_effort(
                self._active_session_id, self._reasoning_effort
            )

    def set_reasoning_effort(self, effort: str | None):
        """Set the active session's reasoning level (in-place, no rebuild)."""
        self._reasoning_effort = effort
        if self._agent is not None:
            self._agent.provider.set_reasoning_effort(effort)
        if self._active_session_id:
            self.sessions.set_reasoning_effort(self._active_session_id, effort)

    def current_model_id(self) -> str:
        return self._model_id

    def current_reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    # ── agent lifecycle ────────────────────────────────────

    def _build_agent(self, model_id: str, cwd: str, mode: str = "default") -> bool:
        entry = self.config.get_model(model_id) if model_id else None
        if entry is None:
            entry = self.config.get_active_model()
        if entry is None or not entry.get("api_key"):
            self._agent = None
            self._builder = None
            self._model_id = ""
            self._reasoning_effort = None
            self._build_key = None
            self._policy = None
            return False
        resolved_id = entry.get("id", model_id)
        # A model switch resets the per-session reasoning level to that model's
        # default; a same-model rebuild (MCP ready, mode change) keeps it.
        if resolved_id != self._model_id:
            self._reasoning_effort = default_for(entry)
        key = (cwd, entry.get("model_name"), mode, model_id)
        if (
            self._build_key == key
            and self._agent is not None
            and self._builder is not None
        ):
            # Nothing relevant changed — keep the built agent. This makes
            # session switching within the same cwd/model/mode instant (the
            # common case) instead of re-spawning MCP + re-rendering.
            return True
        self._build_key = key
        # Policy is scoped to the working directory (always_allow lives in
        # <cwd>/.cluxmate/permissions.json); a cwd change gets a fresh one.
        # Mode is in-memory only, so re-apply the current mode.
        self._policy = PermissionPolicy(cwd)
        self._policy.set_mode(mode)
        llm_provider = _create_provider(entry)
        llm_provider.set_reasoning_effort(self._reasoning_effort)
        builder = AgentBuilder(cwd, llm_provider)
        builder.with_default_tools()
        builder.with_subagent_types(["general-purpose", "explore"])
        builder.with_mode(mode)
        if entry.get("model_name"):
            builder.with_model(entry["model_name"])
        builder.with_context_1m(entry.get("context_1m", False))
        # Share one MCP manager per cwd across builders — load() spawns
        # subprocesses + handshake, so only pay it once per directory.
        builder.with_mcp(self._ensure_mcp(cwd))
        # Subagent logs are persisted through the same JSONL store as the parent.
        builder.with_log_store(self.sessions.log_store)
        self._agent = builder.build(session_log=self._session_log)
        self._builder = builder
        self._model_id = resolved_id
        return True

    def _ensure_mcp(self, cwd: str) -> MCPManager:
        """Get the shared MCP manager for a cwd, loading it in the background.

        The first build for a directory triggers an async load — building the
        agent proceeds immediately with MCP tools absent, and the agent is
        rebuilt with them once the load thread finishes.
        """
        mcp = self._mcp_cache.get(cwd)
        if mcp is None:
            mcp = MCPManager(cwd)
            self._mcp_cache[cwd] = mcp
            t = threading.Thread(
                target=mcp.load, daemon=True, name=f"mcp-load-{cwd[:16]}",
            )
            self._mcp_threads[cwd] = t
            t.start()
        return mcp

    def _maybe_rebuild_after_mcp(self):
        """Rebuild the agent once the background MCP load for its cwd finishes.

        Called at the start of each turn, before refresh_system_prompt. Must
        run first: the rebuilt agent's ToolBridge gets the MCP tools, and the
        system prompt must match the tools the bridge can actually execute.
        """
        if self._builder is None or self._build_key is None:
            return
        cwd = self._build_key[0]
        t = self._mcp_threads.get(cwd)
        if t is None:
            return
        if t.is_alive():
            return
        self._mcp_threads[cwd] = None
        # Load finished (success or fail-soft) — rebuild so MCP tools join the
        # toolset. build() is cheap here: MCPManager.load() is idempotent and
        # has already run; only list_tools() is called.
        self._agent = self._builder.build(session_log=self._session_log)

    # ── agent run ─────────────────────────────────────────

    async def run_prompt(
        self,
        user_message: str,
        history: list[dict],
        on_progress: callable = None,
        on_tool_approval: callable = None,
        on_ask_question: callable = None,
    ) -> AgentResult:
        if self._agent is None:
            raise RuntimeError("No active agent — create or select a session first.")

        if self._builder is not None:
            # Rebuild first (if MCP finished loading) so the system prompt and
            # the agent's tool bridge stay in sync.
            self._maybe_rebuild_after_mcp()
            self._builder.refresh_system_prompt(self._agent)

        # Attach the session log (the agent may have been rebuilt above) and
        # compute this turn's memory/skill injections (empty when unchanged).
        injections: list[tuple[str, str]] = []
        if self._session_log is not None:
            self._agent.session_log = self._session_log
            if self._builder is not None:
                injections = self._builder.injections_for_turn()

        callbacks = None
        if on_progress or on_tool_approval or on_ask_question:
            policy = self._policy

            class _Cb(AgentCallbacks):
                async def on_text_delta(self, chunk: str) -> None:
                    if on_progress:
                        on_progress({"type": "text_delta", "content": chunk})

                async def on_tool_start(
                    self,
                    name: str,
                    params: dict,
                    call_id: str,
                    risk_level: str,
                    categories: frozenset[str] = frozenset(),
                ) -> ToolDecision:
                    # Auto-approved by the policy (mode + always_allow)? Then no
                    # user interaction needed — modes like acceptEdits/yolo are
                    # what make write/dangerous tools run without prompting.
                    # Escalation (danger-full-access) must never auto-approve.
                    escalated = params.get("sandbox_permissions") == "danger-full-access"
                    if policy is not None and policy.is_auto_approved(
                        name, risk_level, escalated=escalated, categories=categories
                    ):
                        return ToolDecision(True, "auto")
                    # Otherwise hand off to the UI's interactive approval
                    # (TUI shows an inline y/n prompt; a None resolver means
                    # the tool can't be approved → deny).
                    if on_tool_approval is not None:
                        approved = bool(await on_tool_approval(
                            name, params, call_id, risk_level,
                        ))
                        return ToolDecision(approved, "user" if approved else "denied")
                    return ToolDecision(False, "denied")

                async def ask_question(
                    self, questions: list[dict], call_id: str
                ) -> dict | None:
                    if on_ask_question is not None:
                        return await on_ask_question(questions, call_id)
                    return None
            callbacks = _Cb()

        result = await self._agent.run(
            user_message, history, callbacks=callbacks, injections=injections,
        )
        if self._agent.compacted_this_turn and self._builder is not None:
            self._builder.invalidate_injections()

        if self._active_session_id and self._session_log is not None:
            # History is persisted incrementally during the turn; flush is a
            # no-op catch-up, then refresh the SQLite side once per turn.
            if self._persister is not None:
                self._persister.flush()
            self.sessions.sync_metadata(self._active_session_id)
            self.sessions.set_title_if_default(
                self._active_session_id, user_message[:80]
            )
        return result
