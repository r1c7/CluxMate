"""SessionLogStore — JSONL persistence for :class:`~cluxmate.core.session_log.SessionLog`.

One append-only ``<id>.jsonl`` file per session under ``~/.cluxmate/sessions/``
(overridable). The first line is the immutable :class:`SessionHeader` tagged
``{"type": "session", ...}``; every subsequent line is one :class:`SessionEvent`
serialized by :func:`~cluxmate.core.session_log.event_to_dict`.

Append-only writes with ``fsync``, contiguous ``seq`` validation, and crash
repair (a torn tail is dropped; an open turn is durably closed with synthetic
``tool/result`` + ``turn/end {interrupted}``). No zstd compression, no
write-batching coordinator, no multi-process exclusion — CluxMate is
single-writer per session.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .session_log import (
    APPEND,
    STAGE_STREAMING,
    STAGE_TOOL_EXECUTING,
    ReplaceOp,
    SessionEvent,
    SessionHeader,
    SessionLog,
    event_from_dict,
    event_to_dict,
)

_logger = logging.getLogger("cluxmate.session_log_store")

#: Error text for a tool call whose result was never durably recorded.
TOOL_OUTCOME_UNKNOWN = (
    "The tool call was interrupted before a result was durably recorded. "
    "Its outcome is unknown. Decide whether to retry from the tool semantics: "
    "retry only if the operation is read-only or idempotent; if it may have "
    "side effects, first verify external state or ask the user."
)


class SessionLogStoreError(Exception):
    """Base for session-log persistence failures."""


class SessionNotFoundError(SessionLogStoreError):
    """The requested session's JSONL artifact does not exist."""


class SessionLogCorruptionError(SessionLogStoreError):
    """The stored log is malformed in a way load cannot repair."""


def _tool_result_message(call_id: str, content: str) -> dict[str, Any]:
    """Build a provider-native tool-result message in the session's API format.

    CluxMate is OpenAI-compatible only (DeepSeek, Qwen, OpenAI, …), so a
    repaired tool result always resumes as an OpenAI-style ``role: "tool"``
    message.
    """
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class SessionLogStore:
    """Append-only JSONL persistence for one session log root."""

    def __init__(self, root_dir: str | Path | None = None):
        if root_dir is None:
            root_dir = Path.home() / ".cluxmate" / "sessions"
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # -- layout ---------------------------------------------------------------

    def path_for(self, session_id: str) -> Path:
        if not session_id or any(c in session_id for c in "/\\") or session_id in (".", ".."):
            raise ValueError(f"invalid session id {session_id!r}")
        return self._root / f"{session_id}.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    # -- write ----------------------------------------------------------------

    def create(self, header: SessionHeader) -> None:
        """Write the header line. Rejects an existing session.

        Written directly (not via a temp-file rename): CluxMate is single-writer
        per session and ids are fresh UUIDs, so cross-process collision safety
        via atomic rename is unnecessary. A crash mid-write
        leaves a torn header line, which :meth:`load` reports as corruption.
        """
        path = self.path_for(header.id)
        if path.exists():
            raise SessionLogStoreError(f"session {header.id!r} already exists")
        line = json.dumps({"type": "session", **header.to_dict()}, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _append_lines(path: Path, lines: list[str]) -> None:
        """Append serialized event lines to ``path`` and fsync (durable)."""
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def append(self, session_id: str, events: list[SessionEvent]) -> None:
        """Durably append a contiguous event batch. Rejects seq gaps vs storage."""
        if not events:
            return
        path = self.path_for(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        expected = self._stored_next_seq(path)
        for offset, event in enumerate(events):
            if event.seq != expected + offset:
                raise SessionLogCorruptionError(
                    f"non-contiguous append to session {session_id!r}: "
                    f"expected seq {expected + offset}, got {event.seq}"
                )
        self._append_lines(
            path,
            [json.dumps(event_to_dict(event), ensure_ascii=False) + "\n" for event in events],
        )

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        """Durably append a single event, trusting the caller's seq watermark.

        Unlike :meth:`append`, this does NOT re-read the file tail to validate
        contiguity — the incremental persister keeps the in-memory watermark
        (single-writer per session), so a per-event tail read-back would be pure
        overhead. The caller's watermark must equal the file's stored next seq.
        """
        path = self.path_for(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        self._append_lines(
            path, [json.dumps(event_to_dict(event), ensure_ascii=False) + "\n"]
        )

    def delete(self, session_id: str) -> None:
        path = self.path_for(session_id)
        if path.exists():
            path.unlink()

    def truncate(self, session_id: str, to_seq: int) -> None:
        """Drop every event with ``seq >= to_seq``, keeping the header + prefix.

        The undo anchor: callers pass the log's pre-turn ``seq`` (captured before
        a turn ran), so this rewinds the conversation to that turn boundary. A
        torn tail is also dropped. No-op when the session file is absent.

        This is the ONE deliberate exception to append-only (D10): it physically
        deletes events. Normal turn flow never truncates — only an explicit user
        undo does, and it rewinds to a turn boundary rather than editing mid-turn.
        """
        path = self.path_for(session_id)
        if not path.exists():
            return
        raw = path.read_bytes()
        lines = raw.decode("utf-8").splitlines()
        kept: list[str] = []
        if not lines:
            return
        kept.append(lines[0])  # header line
        for line in lines[1:]:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail — drop
            if obj.get("seq", 0) < to_seq:
                kept.append(line)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # -- read -----------------------------------------------------------------

    def load(self, session_id: str) -> tuple[SessionHeader, list[SessionEvent]]:
        """Read, validate, and repair a session. Returns (header, events).

        A torn tail (partial final line) is truncated on disk; an open turn is
        durably closed with synthetic ``tool/result`` + ``turn/end {interrupted}``
        closers so the returned history is a valid provider transcript.

        Also self-heals the one corruption the crash-repair can itself produce:
        if a previous ``load()`` ran while a turn was still live, it appended
        synthetic closers mid-turn that the live turn then duplicated with real
        events (two events share a seq). Those spurious closers are dropped here
        so contiguity is restored (see
        :meth:`_drop_premature_interrupted_closers`).
        """
        header, events, torn = self._read(session_id)
        if header.id != session_id:
            raise SessionLogCorruptionError(
                f"header id {header.id!r} does not match requested {session_id!r}"
            )
        repaired = self._drop_premature_interrupted_closers(events)
        if repaired is not events:
            # Rewrite without the spurious closers (also clears any torn tail).
            self._rewrite(self.path_for(session_id), header, repaired)
            events = repaired
        elif torn:
            self._truncate_torn_tail(self.path_for(session_id))
        closers = self._open_turn_closers(header, events)
        if closers:
            self.append(session_id, closers)
            events.extend(closers)
        return header, events

    def inspect(self, session_id: str) -> tuple[SessionHeader, list[SessionEvent]]:
        """Non-mutating read: drops a torn tail in memory but neither truncates
        storage nor appends repair closers."""
        header, events, _ = self._read(session_id)
        return header, events

    # -- internals ------------------------------------------------------------

    def _read(self, session_id: str) -> tuple[SessionHeader, list[SessionEvent], bool]:
        path = self.path_for(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        raw = path.read_bytes()
        torn = bool(raw) and not raw.endswith(b"\n")
        if torn:
            last_nl = raw.rfind(b"\n")
            if last_nl == -1:
                raise SessionLogCorruptionError(
                    f"session {session_id!r} has no complete header line"
                )
            raw = raw[: last_nl + 1]
        lines = raw.decode("utf-8").splitlines()
        if not lines:
            raise SessionLogCorruptionError(f"session {session_id!r} is empty")
        header = self._parse_header(lines[0])
        events: list[SessionEvent] = []
        for i, line in enumerate(lines[1:], start=2):
            try:
                events.append(event_from_dict(json.loads(line)))
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                raise SessionLogCorruptionError(
                    f"corrupt event at line {i} of session {session_id!r}: {e}"
                ) from e
        return header, events, torn

    @staticmethod
    def _parse_header(line: str) -> SessionHeader:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SessionLogCorruptionError(f"unparseable session header: {e}") from e
        if not isinstance(obj, dict) or obj.get("type") != "session":
            raise SessionLogCorruptionError("first line is not a session header")
        try:
            return SessionHeader.from_dict(obj)
        except (TypeError, ValueError) as e:
            raise SessionLogCorruptionError(f"invalid session header: {e}") from e

    def _stored_next_seq(self, path: Path) -> int:
        record = self._read_last_record(path)
        if record is None:
            return 0
        if record.get("type") == "session":
            return 0
        return int(record["seq"]) + 1

    @staticmethod
    def _read_last_record(path: Path) -> dict[str, Any] | None:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            # A single event line fits well within 1 MiB (tool results are capped
            # at ~40k chars); read back far enough to contain the last line.
            f.seek(max(0, size - 1024 * 1024))
            raw = f.read()
        if not raw.endswith(b"\n"):
            raise SessionLogCorruptionError(
                f"session log {path} has a torn tail — load() it first to repair"
            )
        lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])

    @staticmethod
    def _truncate_torn_tail(path: Path) -> None:
        raw = path.read_bytes()
        if raw.endswith(b"\n"):
            return
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            raise SessionLogCorruptionError("cannot truncate: no complete line")
        with open(path, "wb") as f:
            f.write(raw[: last_nl + 1])
            f.flush()
            os.fsync(f.fileno())

    def _open_turn_closers(
        self, header: SessionHeader, events: list[SessionEvent]
    ) -> list[SessionEvent]:
        """Synthetic closers for a turn left open by a crash (empty if balanced)."""
        in_turn = False
        last_turn = 0
        last_step = 0
        open_calls: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.type == "turn/start":
                in_turn = True
                last_turn = event.data.get("turn", 0)
            elif event.type == "turn/end":
                in_turn = False
                open_calls.clear()
            elif event.type == "step/start":
                last_step = event.data.get("step", last_step)
            elif event.type == "tool/call":
                d = event.data
                last_step = d.get("step", last_step)
                open_calls[d["callId"]] = {
                    "turn": d.get("turn", last_turn),
                    "step": d.get("step", last_step),
                    "name": d.get("name", ""),
                }
            elif event.type == "tool/result":
                open_calls.pop(event.data.get("callId"), None)
        if not in_turn:
            return []

        closers: list[SessionEvent] = []
        seq = events[-1].seq + 1 if events else 0
        now = int(time.time() * 1000)
        for call_id, info in open_calls.items():
            data = {
                "turn": info["turn"],
                "step": info["step"],
                "callId": call_id,
                "message": _tool_result_message(call_id, TOOL_OUTCOME_UNKNOWN),
                "error": {"name": "interrupted", "code": "TOOL_OUTCOME_UNKNOWN"},
            }
            closers.append(
                SessionEvent(seq=seq, time=now, type="tool/result", data=data, surfaceOp=APPEND)
            )
            seq += 1
        # Best-effort stage inference: open tool calls mean the crash hit the tool
        # phase (approval or execution — indistinguishable from the log); otherwise
        # it hit LLM generation. Mirrors the writer's precise stage vocabulary.
        stage = STAGE_TOOL_EXECUTING if open_calls else STAGE_STREAMING
        closers.append(
            SessionEvent(
                seq=seq,
                time=now,
                type="turn/end",
                data={
                    "turn": last_turn,
                    "reason": {
                        "kind": "interrupted",
                        "stage": stage,
                        "turn": last_turn,
                        "step": last_step,
                    },
                },
            )
        )
        return closers

    @staticmethod
    def _is_synthetic_tool_result(event: SessionEvent) -> bool:
        """True for a crash-repair ``tool/result`` closer (``TOOL_OUTCOME_UNKNOWN``).

        Only :meth:`_open_turn_closers` ever writes this marker; a real tool result
        is either the tool's output or a ``TOOL_CANCELLED``/``TOOL_DENIED`` error.
        """
        if event.type != "tool/result":
            return False
        err = event.data.get("error")
        return isinstance(err, dict) and err.get("code") == "TOOL_OUTCOME_UNKNOWN"

    @staticmethod
    def _is_synthetic_turn_end(event: SessionEvent) -> bool:
        """True for a crash-repair ``turn/end`` closer (``reason.kind == "interrupted"``).

        The agent loop writes ``kind="aborted"`` for a user/interrupted turn; only
        the store's crash-repair writes ``"interrupted"``, so this is unambiguous.
        """
        if event.type != "turn/end":
            return False
        reason = event.data.get("reason")
        return isinstance(reason, dict) and reason.get("kind") == "interrupted"

    def _drop_premature_interrupted_closers(
        self, events: list[SessionEvent]
    ) -> list[SessionEvent]:
        """Drop crash-repair closers that were inserted while a turn was still live.

        If ``load()`` ran concurrently with an in-flight turn (e.g. a caller that
        should have used :meth:`inspect`), the crash-repair appended a synthetic
        ``tool/result`` + ``turn/end {interrupted}`` closer block, and the live turn
        then re-claimed those same seqs with real events — a duplicate seq that
        breaks the contiguous-seq invariant on every later load.

        A closer block is *premature* when the ``turn/end {interrupted}`` is
        followed by anything other than a ``turn/start`` (or end-of-file): a turn
        that truly ended at the closers is either the last thing in the file or is
        followed by the next turn's ``turn/start``. Dropping the premature block
        restores contiguity without renumbering, because the live turn re-used the
        exact seq range the block occupied.

        Returns ``events`` unchanged when nothing is premature.
        """
        drop = [False] * len(events)
        for i, event in enumerate(events):
            if not self._is_synthetic_turn_end(event):
                continue
            nxt = events[i + 1] if i + 1 < len(events) else None
            if nxt is None or nxt.type == "turn/start":
                continue  # genuine end of turn (EOF or next turn started)
            # The turn did not end here — drop the whole synthetic closer block,
            # including any synthetic tool/result closers immediately preceding it.
            j = i
            while j > 0 and self._is_synthetic_tool_result(events[j - 1]):
                j -= 1
            for k in range(j, i + 1):
                drop[k] = True
        if not any(drop):
            return events
        return [e for i, e in enumerate(events) if not drop[i]]

    def _rewrite(
        self, path: Path, header: SessionHeader, events: list[SessionEvent]
    ) -> None:
        """Rewrite ``path`` with ``header`` + ``events`` verbatim (self-heal).

        Used only to remove spurious crash-repair closers inserted by a concurrent
        ``load()``. Not atomic (single-writer per session); a crash mid-rewrite is
        recovered by the next ``load()``'s torn-tail handling.
        """
        lines = [json.dumps({"type": "session", **header.to_dict()}, ensure_ascii=False)]
        lines += [json.dumps(event_to_dict(e), ensure_ascii=False) for e in events]
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


class IncrementalPersister:
    """Persist each event of a :class:`SessionLog` as it is appended.

    Wires :meth:`SessionLog.on_append` so a crash mid-turn leaves every already
    committed event on disk; :meth:`SessionLogStore.load` then repairs the open
    turn with synthetic ``tool/result`` + ``turn/end {interrupted}`` closers.
    The seq watermark is kept in memory (single-writer per session) instead of
    re-reading the file tail on every append.

    Each event is written and fsynced synchronously on the thread that appends
    it. The event rate is low (tens per turn, turns spaced by LLM/tool latency),
    so the durability is worth the per-event fsync; the alternative — flushing
    only at turn end — is exactly what loses a half-finished turn when the
    process is killed.
    """

    def __init__(self, store: "SessionLogStore", session_id: str, log: SessionLog):
        self._store = store
        self._session_id = session_id
        self._log = log
        self._next_seq = log.seq
        self._dispose = log.on_append(self._on_event)

    @property
    def persisted_seq(self) -> int:
        """Number of events durably written (== the store's stored next seq)."""
        return self._next_seq

    def _on_event(self, event: SessionEvent) -> None:
        if event.seq != self._next_seq:
            # The log assigns monotonic seqs, so this only fires when the store
            # and log fell out of sync (e.g. an external truncation without a
            # rebind). Re-anchor and skip rather than writing a gap that load()
            # would report as corruption.
            _logger.warning(
                "session %r seq mismatch: log emitted %d, persister expected %d",
                self._session_id, event.seq, self._next_seq,
            )
            self._next_seq = event.seq + 1
            return
        try:
            self._store.append_event(self._session_id, event)
        except Exception:
            # Persistence is best-effort: an I/O failure must never break the
            # agent turn (matches the prior turn-end flush's contract).
            _logger.warning(
                "failed to persist event %d for session %r",
                event.seq, self._session_id, exc_info=True,
            )
        self._next_seq = event.seq + 1

    def flush(self) -> None:
        """Persist any events not yet written (catch-up; normally a no-op)."""
        try:
            while self._next_seq < self._log.seq:
                self._store.append_event(
                    self._session_id, self._log.events[self._next_seq]
                )
                self._next_seq += 1
        except Exception:
            _logger.warning("failed to flush session %r", self._session_id, exc_info=True)

    def dispose(self) -> None:
        """Detach the observer so appends to the (replaced) log stop flushing."""
        self._dispose()


def _message_text(message: dict[str, Any]) -> str:
    """Extract the plain-text payload from a provider-native message.

    CluxMate is OpenAI-compatible only, so ``content`` is a string, a list of
    text blocks, or absent. Returns "" for tool_use messages (no text) and
    anything unparseable.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _blocks_from_events(events: list[SessionEvent]) -> list[dict[str, Any]]:
    """Reconstruct a subagent's ordered text/tool blocks from its raw log.

    ``assistant/message`` text becomes a text block; ``tool/call`` becomes a
    running tool block that the paired ``tool/result`` then patches to
    done/error + result. This mirrors the desktop's interleaved block model.
    """
    blocks: list[dict[str, Any]] = []
    tool_index: dict[str, int] = {}
    for event in events:
        if event.type == "assistant/message":
            text = _message_text(event.data.get("message") or {})
            if text:
                blocks.append({"type": "text", "text": text})
        elif event.type == "tool/call":
            call_id = event.data.get("callId")
            blocks.append({
                "type": "tool",
                "tool": {
                    "call_id": call_id,
                    "name": event.data.get("name", ""),
                    "input": event.data.get("input", {}),
                    # risk isn't logged; "write" is a neutral display default.
                    "risk_level": "write",
                    "status": "running",
                },
            })
            if call_id is not None:
                tool_index[call_id] = len(blocks) - 1
        elif event.type == "tool/result":
            idx = tool_index.get(event.data.get("callId"))
            if idx is not None:
                tool = blocks[idx]["tool"]
                tool["status"] = "error" if event.data.get("error") else "done"
                tool["result"] = _message_text(event.data.get("message") or {})
    return blocks


def _replay_node(
    store: "SessionLogStore", child_id: str, spawn: dict[str, Any], root_turn: int
) -> list[dict[str, Any]]:
    """Reconstruct one subagent node (and recursively its descendants)."""
    try:
        # inspect() (NOT load()): replay is a read-only view, so it must not
        # repair/append closers — an in-flight turn would otherwise be marked
        # interrupted (and its live persister would collide on a duplicate seq).
        _header, events = store.inspect(child_id)
    except SessionNotFoundError:
        return []  # header written but never run / already deleted — skip

    blocks = _blocks_from_events(events)
    thinking = "".join(
        event.data.get("reasoning") or ""
        for event in events
        if event.type == "assistant/message"
    )
    prompt = ""
    for event in events:
        if event.type == "user/message" and event.data.get("source") == "human":
            prompt = _message_text(event.data.get("message") or {})
            break
    result = ""
    for event in reversed(events):
        if event.type == "assistant/message":
            result = _message_text(event.data.get("message") or {})
            break
    reason_kind = "interrupted"  # no turn/end => crashed mid-turn
    for event in events:
        if event.type == "turn/end":
            reason = event.data.get("reason") or {}
            reason_kind = (
                reason.get("kind", "completed")
                if isinstance(reason, dict)
                else str(reason)
            )
    status = "done" if reason_kind in ("completed", "max-tokens", "max-turns") else "error"

    # Reconstruct this subagent's cumulative token usage so the desktop can fold
    # it into the parent session's input/output totals. Each assistant/message
    # event logs its LLM call's `usage` (input_tokens/output_tokens/cache_*) and
    # `timing` (out_tokens); output_tokens is the per-call completion count.
    input_tokens = 0
    output_tokens = 0
    for event in events:
        if event.type != "assistant/message":
            continue
        usage = event.data.get("usage") or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0

    node: dict[str, Any] = {
        "turn": root_turn,
        "agent_id": child_id,
        "parent_id": spawn.get("parent_id", "root"),
        "subagent_type": spawn.get("subagent_type", ""),
        "description": spawn.get("description", ""),
        "depth": spawn.get("depth", 1),
        "status": status,
        "blocks": blocks,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if result:
        node["result"] = result
    if prompt:
        node["prompt"] = prompt
    if thinking:
        node["thinking"] = thinking

    nodes = [node]
    for event in events:
        if event.type == "subagent/spawn":
            gc_id = event.data.get("session_id")
            if gc_id:
                nodes.extend(_replay_node(store, gc_id, event.data, root_turn))
    return nodes


def replay_subagents(store: "SessionLogStore", session_id: str) -> list[dict[str, Any]]:
    """Reconstruct the flat subagent tree for a session from its JSONL logs.

    Walks the parent log's ``subagent/spawn`` events (each points at a child
    ``<id>.jsonl``), recursively rebuilding every node into the desktop's
    ``AgentNode`` shape plus a ``turn`` field — the parent-session turn whose
    agent message owns the whole subtree. Returns an empty list when the session
    is missing or spawned no subagents.
    """
    try:
        # inspect() (NOT load()): replay is read-only — see _replay_node.
        _header, events = store.inspect(session_id)
    except SessionNotFoundError:
        return []
    nodes: list[dict[str, Any]] = []
    turn = 0
    for event in events:
        if event.type == "turn/start":
            turn = event.data.get("turn", turn)
        elif event.type == "subagent/spawn":
            child_id = event.data.get("session_id")
            if child_id:
                # Prefer the stored turn (newer logs record it on the spawn
                # event); fall back to the walked turn/start for older logs.
                node_turn = event.data.get("turn", turn)
                nodes.extend(_replay_node(store, child_id, event.data, node_turn))
    return nodes


def subagent_session_ids(store: "SessionLogStore", session_id: str) -> set[str]:
    """All subagent session ids reachable from a session's spawn events.

    Depth-first walk of the ``subagent/spawn`` pointers (each child log can spawn
    its own descendants). Used to cascade-delete a subtree of child logs.
    """
    ids: set[str] = set()
    try:
        # inspect() (NOT load()): finding descendants must not repair child logs.
        _header, events = store.inspect(session_id)
    except SessionNotFoundError:
        return ids
    for event in events:
        if event.type == "subagent/spawn":
            child_id = event.data.get("session_id")
            if child_id and child_id not in ids:
                ids.add(child_id)
                ids |= subagent_session_ids(store, child_id)
    return ids


def orphaned_subagent_ids(
    store: "SessionLogStore", session_id: str, to_seq: int
) -> set[str]:
    """Subagent session ids orphaned by truncating a log to ``to_seq``.

    A ``subagent/spawn`` event with ``seq >= to_seq`` is about to be dropped, so
    its child log — and that child's whole descendant subtree — becomes
    unreachable. Returns those ids so the caller can delete them alongside the
    truncation (an undo should fully revert, not leak orphaned ``<child>.jsonl``).
    """
    ids: set[str] = set()
    try:
        # inspect() (NOT load()): the orphan scan must not repair the logs it reads.
        _header, events = store.inspect(session_id)
    except SessionNotFoundError:
        return ids
    for event in events:
        if event.type == "subagent/spawn" and event.seq >= to_seq:
            child_id = event.data.get("session_id")
            if child_id and child_id not in ids:
                ids.add(child_id)
                ids |= subagent_session_ids(store, child_id)
    return ids


__all__ = [
    "TOOL_OUTCOME_UNKNOWN",
    "IncrementalPersister",
    "SessionLogStore",
    "SessionLogStoreError",
    "SessionNotFoundError",
    "SessionLogCorruptionError",
    "replay_subagents",
    "subagent_session_ids",
    "orphaned_subagent_ids",
]
