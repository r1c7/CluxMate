"""Lifecycle hooks — user-defined commands run at agent events.

Claude-Code-style hooks: a user configures shell commands that run at fixed
points in the agent lifecycle, receive a JSON payload on stdin, and influence
the agent by writing JSON to stdout:

- ``{"decision": "block", "reason": "..."}`` (or ``{"continue": false, ...}``)
  → block the action; the reason is fed back to the model / shown to the user.
- ``{"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}``
  → continue, but inject the extra context for the model.
- exit code 2 → block (Claude Code's "blocking error" convention).
- anything else (no output / non-JSON / exit 0) → continue with no effect.

Events (the full surface — see docs/plans/hooks.md for the decision record):
    UserPromptSubmit   before the model sees a human prompt (can block/inject)
    PreToolUse         before a tool runs (can block/inject)
    PostToolUse        after a tool runs (inject only — too late to block)
    Stop               after the agent finishes a turn (can block → re-run,
                       up to 3 retries; inject)
    SessionStart       once per session start (can block → aborts startup;
                       inject → prepended to the first turn)
    SessionEnd         at session shutdown (output discarded — nothing left
                       to block or feed)
    SubagentStop       after a subagent finishes (can block → the block
                       reason replaces the subagent's reply in the parent;
                       inject → appended to the reply)
    PreCompact         before an auto-compaction runs (can block → skip
                       compaction this step; inject → context for the model)
    Notification       fire-and-forget side effects, triggered manually
                       (hooks/notify RPC) or at turn end (desktop); output
                       is discarded entirely

Trust model: hooks are the USER's own configuration, not model output, so they
run with ``subprocess.run`` at normal integrity (NOT the Low-IL / bwrap sandbox
that guards model-generated bash). They are still bounded — per-hook timeout and
a stdout/stderr cap — and a crash/timeout is a no-op, never a turn failure.

Config lives at ``<cwd>/.cluxmate/settings.json`` (project) and
``~/.cluxmate/settings.json`` (global); project entries run after global ones.
Schema::

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "bash|write_file|multi_edit|search_replace|delete_file",
            "hooks": [
              {"type": "command", "command": "python .cluxmate/hooks/audit.py", "timeout": 30}
            ]
          }
        ],
        "PostToolUse": [
          {"hooks": [{"type": "command", "command": "node .cluxmate/hooks/notify.js"}]}
        ]
      }
    }

``matcher`` is a tool-name regex (absent → matches every tool); it is ignored
for events that carry no tool (UserPromptSubmit / Stop).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Default per-hook wall-clock bound. A slow hook must not stall the turn; the
# loop runs hooks on a threadpool so the bound also caps the user's wait.
DEFAULT_TIMEOUT_S = 60.0
# Cap on combined stdout+stderr read back from a hook (defense in depth — a
# misbehaving hook must not dump megabytes into memory).
MAX_OUTPUT_CHARS = 64_000


def _duration(start: float, info: dict[str, Any]) -> dict[str, Any]:
    """Attach wall-clock duration (ms) to a hook-run info dict."""
    info["duration_ms"] = int((time.monotonic() - start) * 1000)
    return info


@dataclass
class HookResult:
    """Aggregated outcome of one event across all matched hooks."""

    blocked: bool = False
    reason: str = ""
    feedback: list[str] = field(default_factory=list)


@dataclass
class _HookSpec:
    """One parsed hook command entry."""

    event: str
    command: str
    timeout: float
    matcher: re.Pattern[str] | None = None
    # The raw matcher string from config (None when no matcher). Kept verbatim so
    # ``list_hooks`` can surface it without re-serializing the compiled regex.
    matcher_raw: str | None = None

    def matches(self, tool_name: str | None) -> bool:
        if self.matcher is None:
            return True
        if not tool_name:
            return False
        return self.matcher.search(tool_name) is not None


class HookManager:
    """Loads hook config and runs matched commands for an event.

    Thread-safe for reads after construction; construction does best-effort file
    I/O (a missing/corrupt settings.json yields an empty hook set, never raises).
    """

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
        self.session_id = ""
        self._specs: dict[str, list[_HookSpec]] = self._load()
        # Optional transport observer — called with ("hook_start"|"hook_result",
        # data) before/after each hook command runs. The JSON-RPC server wires it
        # to emit chat/stream events; headless/CLI leave it unset.
        self._observer: Callable[[str, dict[str, Any]], None] | None = None

    # ── config ────────────────────────────────────────────────

    def _load(self) -> dict[str, list[_HookSpec]]:
        merged: dict[str, list[_HookSpec]] = {}
        for path, _label in self._roots():
            data = self._read_json(path)
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                continue
            for event, entries in hooks.items():
                if not isinstance(event, str) or not isinstance(entries, list):
                    continue
                for entry in entries:
                    spec = self._parse_entry(event, entry)
                    if spec is not None:
                        merged.setdefault(event, []).append(spec)
        return merged

    def _roots(self) -> list[tuple[Path, str]]:
        return [
            (Path.home() / ".cluxmate" / "settings.json", "global"),
            (Path(self._cwd) / ".cluxmate" / "settings.json", "project"),
        ]

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _parse_entry(self, event: str, entry: Any) -> _HookSpec | None:
        if not isinstance(entry, dict):
            return None
        hook_list = entry.get("hooks")
        if not isinstance(hook_list, list) or not hook_list:
            return None
        # v1 runs the FIRST command in each entry's hook list (the common
        # single-command case). A multi-hook entry would need an ordering
        # contract; keep the schema but run one.
        hook = hook_list[0]
        if not isinstance(hook, dict):
            return None
        if hook.get("type") != "command":
            return None
        command = hook.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        matcher: re.Pattern[str] | None = None
        matcher_raw: str | None = None
        raw_matcher = entry.get("matcher")
        if isinstance(raw_matcher, str) and raw_matcher.strip():
            matcher_raw = raw_matcher.strip()
            try:
                matcher = re.compile(matcher_raw)
            except re.error:
                matcher = None  # a bad regex disables matching (never raises)
        timeout = DEFAULT_TIMEOUT_S
        raw_timeout = hook.get("timeout")
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
            timeout = float(raw_timeout)
        return _HookSpec(event=event, command=command.strip(),
                         timeout=timeout, matcher=matcher, matcher_raw=matcher_raw)

    # ── query ─────────────────────────────────────────────────

    def has_event(self, event: str) -> bool:
        return bool(self._specs.get(event))

    def reload(self) -> None:
        """Re-read settings.json (global + project) in place.

        Lets the user edit hooks and apply them WITHOUT restarting the session:
        the same HookManager instance is kept (so the builder/agent references,
        session_id, and observer stay valid), only ``self._specs`` is replaced.
        The rebind is atomic under the GIL — an in-flight turn that already
        grabbed the old list keeps using it; the new specs apply from the next
        hook event.
        """
        self._specs = self._load()

    def set_observer(
        self, observer: Callable[[str, dict[str, Any]], None] | None
    ) -> None:
        """Attach a transport observer for ``hook_start``/``hook_result``.

        Called with ``(kind, data)`` before/after each hook command runs. The
        observer runs on the turn's event loop; exceptions are swallowed so a
        broken transport can never fail the hook or the turn.
        """
        self._observer = observer

    def list_hooks(self) -> list[dict[str, Any]]:
        """Normalized view of every active hook (for hooks/get + debugging)."""
        out: list[dict[str, Any]] = []
        for event, specs in self._specs.items():
            for spec in specs:
                out.append({
                    "event": spec.event,
                    "matcher": spec.matcher_raw,
                    "command": spec.command,
                    "timeout": spec.timeout,
                })
        return out

    def _notify(self, kind: str, data: dict[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(kind, data)
        except Exception:
            pass

    # ── execution ─────────────────────────────────────────────

    async def run_event(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_response: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> HookResult:
        """Run every hook matching ``event`` (and ``tool_name``, when given).

        ``extra`` carries event-specific payload fields merged into the stdin
        JSON: ``source`` (SessionStart), ``reason`` (SessionEnd),
        ``subagent_id``/``subagent_type``/``task_description``/``error``
        (SubagentStop), ``trigger``/``custom_instructions`` (PreCompact), and
        ``message`` (Notification).

        Never raises: individual hook failures are swallowed so a broken user
        hook degrades to a no-op rather than killing the turn. Returns the
        aggregated block/feedback across all matched hooks (the first block
        wins; all feedback is collected). Callers that want fire-and-forget
        semantics (Notification, SessionEnd) simply discard the result.
        """
        specs = [s for s in self._specs.get(event, []) if s.matches(tool_name)]
        if not specs:
            return HookResult()
        payload = self._payload(event, tool_name, tool_input, tool_response, prompt, response)
        if extra:
            payload.update(extra)
        result = HookResult()
        for spec in specs:
            self._notify("hook_start", {
                "event": event, "tool_name": tool_name, "command": spec.command,
            })
            one, info = await self._run_one(spec, payload)
            self._notify("hook_result", {
                "event": event, "tool_name": tool_name, "command": spec.command,
                "blocked": one.blocked, "reason": one.reason,
                "feedback": one.feedback, **info,
            })
            if one.blocked:
                result.blocked = True
                if one.reason and not result.reason:
                    result.reason = one.reason
            result.feedback.extend(one.feedback)
        return result

    def _payload(
        self,
        event: str,
        tool_name: str | None,
        tool_input: dict[str, Any] | None,
        tool_response: str | None,
        prompt: str | None,
        response: str | None,
    ) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cwd": self._cwd,
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
            "prompt": prompt,
            "response": response,
        }

    async def _run_one(
        self, spec: _HookSpec, payload: dict[str, Any]
    ) -> tuple[HookResult, dict[str, Any]]:
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        info: dict[str, Any] = {"exit_code": None, "error": None}
        try:
            rc, out, err = await loop.run_in_executor(
                None, self._run_command, spec, payload
            )
            info["exit_code"] = rc
        except Exception as exc:
            # Spawn failure / timeout / I/O error → no-op, but report it so the
            # observer (desktop) can surface the failure rather than silence it.
            info["error"] = str(exc)
            return HookResult(), _duration(start, info)
        result = self._interpret(spec, rc, out, err)
        return result, _duration(start, info)

    def _run_command(
        self, spec: _HookSpec, payload: dict[str, Any]
    ) -> tuple[int, str, str]:
        env = dict(os.environ)
        env["CLUXMATE_HOOK_EVENT"] = spec.event
        proc = subprocess.run(
            spec.command,
            shell=True,
            cwd=self._cwd,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    @staticmethod
    def _interpret(spec: _HookSpec, rc: int, out: str, err: str) -> HookResult:
        result = HookResult()
        text = out.strip()
        # exit code 2 = blocking error (Claude Code convention).
        if rc == 2:
            result.blocked = True
            result.reason = (
                text or err.strip() or f"[hook blocked: {spec.command} exited 2]"
            )
            return result
        if not text:
            return result
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS]
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return result  # non-JSON stdout is not feedback
        if not isinstance(data, dict):
            return result
        if data.get("decision") == "block" or data.get("continue") is False:
            result.blocked = True
            result.reason = str(data.get("reason") or "").strip() or (
                f"[hook blocked: {spec.command}]"
            )
            return result
        hso = data.get("hookSpecificOutput")
        if isinstance(hso, dict):
            ctx = hso.get("additionalContext")
            if isinstance(ctx, str) and ctx.strip():
                result.feedback.append(ctx.strip())
        return result
