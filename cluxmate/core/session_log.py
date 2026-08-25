"""Event-sourced session log — the append-only source of truth for an agent turn.

Mirrors the DeepSeek Harness ``dsh-session`` design, simplified for CluxMate:

- A :class:`SessionLog` is an append-only sequence of :class:`SessionEvent` objects.
  The provider message history is *derived* from it (never stored separately), so
  prompt-cache reconstruction and agent replay share one authoritative source.
- A **surface** (an ordered projection of message-producing events) is maintained on
  top of the raw log. Compaction rewrites the surface via ``replace`` surface ops
  without deleting the underlying events, preserving a reusable request prefix
  (KV-cache friendly) while the raw log stays append-only.
- ``request/header`` events record the full non-history request envelope (call
  config + system prompt + tool schemas) with a reason ``initial`` / ``change``.
  Only changed headers are appended, so the *delta* of a turn is the diff between
  consecutive headers plus any synthetic ``user/message`` injections.

Model-visible ⟺ logged: the exact request sent for any turn is
``[{"role": "system", ...}] + derive_messages()`` — the system message comes from
``fold_request_header`` and the messages from the surface projection. The two
must be combined: ``derive_messages()`` deliberately excludes the system prompt.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from .context import estimate_tokens

_logger = logging.getLogger("cluxmate.session")

# The on-disk format version. Bumped only for structural changes (header shape,
# event envelope, surface mechanism). Pre-release: no compatibility promise.
SESSION_FORMAT_VERSION = 0

# Literal surface op meaning "append to the tail of the surface".
APPEND = "append"

# Event types that produce a model-visible message and therefore live on the
# ordered surface. Anything else is log-only (boundaries, usage, headers).
SURFACE_EVENT_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

# Interruption stages recorded on ``turn/end.reason.stage``. Shared by the writer
# (the agent loop, which records the precise stage) and the crash-repair reader
# (SessionLogStore, which infers a best-effort stage) so the vocabulary never
# drifts. ``approval`` is writer-only: a hard crash cannot be distinguished
# between "waiting for approval" and "executing", so the reader folds both into
# ``tool_executing``.
STAGE_STREAMING = "streaming"
STAGE_APPROVAL = "approval"
STAGE_TOOL_EXECUTING = "tool_executing"


# ── lossless JSON ──────────────────────────────────────────────────────────
#
# Durable values need one accepted representation, not a check followed by a
# second read. snapshot_json_value() validates and deep-copies in one pass and
# raises on invalid input; is_json_value() is the boolean predicate. Recursion is
# acceptable here: CluxMate message graphs are shallow (a few dozen levels at
# most), unlike DSH's call-stack-defensive iterative walk.


def snapshot_json_value(value: Any) -> Any:
    """Deep-copy ``value`` while validating it is lossless JSON. Raises ``ValueError``
    on any non-JSON value (cycles, non-string dict keys, NaN/Inf, bytes, tuples,
    sets, custom objects). Returns the detached copy."""
    seen: set[int] = set()

    def walk(v: Any) -> Any:
        if v is None or isinstance(v, (bool, str, int)):
            return v
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError(f"non-finite float {v!r} is not JSON")
            return v
        if isinstance(v, list):
            vid = id(v)
            if vid in seen:
                raise ValueError("circular reference is not JSON")
            seen.add(vid)
            try:
                return [walk(item) for item in v]
            finally:
                seen.remove(vid)
        if isinstance(v, dict):
            vid = id(v)
            if vid in seen:
                raise ValueError("circular reference is not JSON")
            seen.add(vid)
            try:
                out: dict[str, Any] = {}
                for key, item in v.items():
                    if not isinstance(key, str):
                        raise ValueError(f"non-string dict key {key!r} is not JSON")
                    out[key] = walk(item)
                return out
            finally:
                seen.remove(vid)
        raise ValueError(f"unsupported JSON type {type(v).__name__}")

    return walk(value)


def is_json_value(value: Any) -> bool:
    """True when ``value`` is a plain lossless-JSON value (validated in one pass)."""
    try:
        snapshot_json_value(value)
    except ValueError:
        return False
    return True


# ── surface op ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplaceOp:
    """Replace surface nodes ``[start, end]`` (inclusive) with one node."""

    start: int
    end: int


# ``surfaceOp`` is either the literal ``"append"`` or a :class:`ReplaceOp`.
SurfaceOp = Any


# ── header ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionHeader:
    """Immutable session metadata, kept outside the event log.

    ``provider`` / ``model`` are durable because they decide the resumed session's
    tools and prompt — restoring a different composition would replay history the
    model can no longer act on.
    """

    id: str
    createdAt: int
    version: int = SESSION_FORMAT_VERSION
    cwd: str | None = None
    provider: str = ""
    model: str = ""
    apiType: str = ""  # "openai" — decides the resumed API format
    parentSession: str | None = None
    seedLength: int | None = None
    origin: str | None = None  # "subagent" | None
    delegationDepth: int | None = None

    def __post_init__(self) -> None:
        if self.version != SESSION_FORMAT_VERSION:
            raise ValueError(
                f"unsupported session format version {self.version!r} "
                f"(expected {SESSION_FORMAT_VERSION})"
            )
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("header.id is required")
        if not isinstance(self.createdAt, int) or self.createdAt < 0:
            raise ValueError("header.createdAt must be a non-negative int (ms)")
        if self.origin not in (None, "subagent"):
            raise ValueError(f"invalid header.origin {self.origin!r}")

    def to_dict(self) -> dict[str, Any]:
        """Canonical JSON form; absent optional fields are omitted."""
        d: dict[str, Any] = {
            "version": self.version,
            "id": self.id,
            "createdAt": self.createdAt,
        }
        if self.cwd is not None:
            d["cwd"] = self.cwd
        if self.provider:
            d["provider"] = self.provider
        if self.model:
            d["model"] = self.model
        if self.apiType:
            d["apiType"] = self.apiType
        if self.parentSession is not None:
            d["parentSession"] = self.parentSession
        if self.seedLength is not None:
            d["seedLength"] = self.seedLength
        if self.origin is not None:
            d["origin"] = self.origin
        if self.delegationDepth is not None:
            d["delegationDepth"] = self.delegationDepth
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionHeader":
        """Build from a JSON object (a stray ``type`` tag is ignored)."""
        fields = {k: v for k, v in data.items() if k != "type"}
        return cls(**fields)


# ── event ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionEvent:
    """One immutable entry in the session log.

    ``data`` is the detached, validated JSON payload (deep-copied at append). It
    is owned by the log and must not be mutated by callers. ``surfaceOp`` /
    ``sourceEventSeqs`` are present only on :data:`SURFACE_EVENT_TYPES` events.
    """

    seq: int
    time: int
    type: str
    data: Mapping[str, Any]
    surfaceOp: SurfaceOp | None = None
    sourceEventSeqs: tuple[int, ...] | None = None
    ignorable: bool = False


# ── request header reconstruction ───────────────────────────────────────────


def canonical_header(header: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a ``request/header`` value to canonical form: an empty system
    prompt and empty tool list become absent fields, matching how requests are
    built. Logging, folding, and comparison use this one representation."""
    out: dict[str, Any] = {"config": header["config"]}
    system = header.get("system")
    tools = header.get("tools")
    if isinstance(system, str) and system:
        out["system"] = system
    if tools:
        out["tools"] = tools
    return out


def _tools_equal(a: Any, b: Any) -> bool:
    """Ordered JSON equality for two tool-schema lists (empty == empty)."""
    ta = a or []
    tb = b or []
    if len(ta) != len(tb):
        return False
    return all(
        json.dumps(x, sort_keys=True) == json.dumps(y, sort_keys=True)
        for x, y in zip(ta, tb)
    )


def header_equals(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Field-wise equality over canonical headers. Tool schemas compare in order."""
    ca = canonical_header(a)
    cb = canonical_header(b)
    if json.dumps(ca.get("config"), sort_keys=True) != json.dumps(
        cb.get("config"), sort_keys=True
    ):
        return False
    if ca.get("system") != cb.get("system"):
        return False
    return _tools_equal(ca.get("tools"), cb.get("tools"))


def diff_headers(
    prev: Mapping[str, Any] | None, cur: Mapping[str, Any]
) -> dict[str, Any]:
    """Field-wise diff between two request headers; ``prev`` may be ``None``.

    Returns a JSON-serializable dict of what changed (empty when equal). ``config``
    differences carry ``{old, new}`` per changed scalar field; ``system``/``tools``
    report only that they changed (their full text/schemas live in the header).
    """
    prev_c = canonical_header(prev) if prev is not None else None
    cur_c = canonical_header(cur)
    changes: dict[str, Any] = {}

    prev_cfg = (prev_c.get("config") or {}) if prev_c else {}
    cur_cfg = cur_c.get("config") or {}
    cfg_changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(prev_cfg) | set(cur_cfg)):
        pv = prev_cfg.get(key)
        cv = cur_cfg.get(key)
        if pv != cv:
            cfg_changes[key] = {"old": pv, "new": cv}
    if cfg_changes:
        changes["config"] = cfg_changes

    prev_sys = prev_c.get("system") if prev_c else None
    if prev_sys != cur_c.get("system"):
        changes["system_changed"] = True
    if not _tools_equal(prev_c.get("tools") if prev_c else None, cur_c.get("tools")):
        changes["tools_changed"] = True
    return changes


def fold_request_header(
    events: Iterator[SessionEvent], from_state: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Fold the ``request/header`` events of a log (or prefix) into the header in
    force after the last snapshot. Non-header events are skipped. Returns None
    when no header exists yet."""
    state = from_state
    for event in events:
        if event.type == "request/header":
            state = canonical_header(event.data["header"])
    return state


def reconstruct_turn_contexts(events: list[SessionEvent]) -> list[dict[str, Any]]:
    """Reconstruct the exact per-step request context of every turn from a raw log.

    Replays the append-only event log sequentially, maintaining the running
    message surface and the request header in force, and snapshots the surface at
    every ``step/start`` — not just each turn's first step. This is the read-side
    inverse of the D7 invariant ("Model-visible ⟺ logged"): for any step, the
    exact request is ``[{"role":"system","content":system}] + messages`` (the
    surface at that step's ``step/start``, after any compaction already applied,
    plus the header in force).

    Returns one dict per turn:
    ``{"turn", "system", "tools", "config", "steps"}`` where ``system``/``tools``/
    ``config`` are the turn's (constant) request envelope and ``steps`` is one
    entry per LLM call:
    ``{"step", "messages", "sources", "compactions", "tokens_estimate"}``.
    ``sources`` is parallel to ``messages`` and marks each message's origin
    ("human" | "memory" | "skill" | "mode" | "compaction" | "interruption", or
    None for assistant/tool). ``compactions`` lists each compaction summary
    present in that step's surface: ``{"index", "turn", "step", "shadowed"}``
    where ``index`` is its position in ``messages``, ``turn``/``step`` identify
    which step performed the compaction, and ``shadowed`` are the messages that
    were replaced (still in the raw log but no longer model-visible).
    """
    # seq -> message for resolving a compaction's shadowed source_event_seqs.
    seq_to_message: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.type in SURFACE_EVENT_TYPES:
            seq_to_message[event.seq] = event.data["message"]

    # Each surface entry carries its message, source, and (for compaction
    # summaries) the compaction metadata so a snapshot can project all three.
    surface: list[dict[str, Any]] = []
    header: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []
    pending_step: dict[str, Any] | None = None
    pending_compaction: dict[str, Any] | None = None  # compaction awaiting its step
    turn = 0
    for event in events:
        if event.type == "turn/start":
            turn = event.data.get("turn", 0)
        elif event.type == "step/start":
            step = event.data.get("step", 0)
            # A compaction logged just before this step/start prepares this step.
            if pending_compaction is not None:
                pending_compaction["step"] = step
                pending_compaction = None
            pending_step = {
                "turn": turn,
                "step": step,
                "surface": list(surface),
                "header": header,
            }
            steps.append(pending_step)
        elif event.type == "request/header":
            header = canonical_header(event.data["header"])
            # The header fires right after step/start for the SAME step; finalize
            # that step's snapshot (surface already captured, header was pending).
            if pending_step is not None:
                pending_step["header"] = header
        elif event.type in SURFACE_EVENT_TYPES:
            source = event.data.get("source") if event.type == "user/message" else None
            entry: dict[str, Any] = {"message": event.data["message"], "source": source, "compaction": None}
            if event.surfaceOp == APPEND:
                surface.append(entry)
            elif isinstance(event.surfaceOp, ReplaceOp):
                op = event.surfaceOp
                if source == "compaction":
                    shadowed = [
                        seq_to_message[s]
                        for s in (event.sourceEventSeqs or ())
                        if s in seq_to_message
                    ]
                    entry["compaction"] = {"turn": turn, "step": None, "shadowed": shadowed}
                    pending_compaction = entry["compaction"]
                surface[op.start : op.end + 1] = [entry]

    # Group step snapshots by turn, projecting each surface into messages/sources.
    turns: dict[int, dict[str, Any]] = {}
    for snap in steps:
        t = snap["turn"]
        turn_entry = turns.get(t)
        if turn_entry is None:
            h = snap["header"] or {}
            turn_entry = {
                "turn": t,
                "system": h.get("system"),
                "tools": h.get("tools", []),
                "config": h.get("config", {}),
                "steps": [],
            }
            turns[t] = turn_entry
        messages: list[dict[str, Any]] = []
        sources: list[str | None] = []
        compactions: list[dict[str, Any]] = []
        for i, entry in enumerate(snap["surface"]):
            messages.append(entry["message"])
            sources.append(entry["source"])
            if entry["compaction"] is not None:
                compactions.append({
                    "index": i,
                    "turn": entry["compaction"]["turn"],
                    "step": entry["compaction"]["step"],
                    "shadowed": entry["compaction"]["shadowed"],
                })
        turn_entry["steps"].append({
            "step": snap["step"],
            "messages": messages,
            "sources": sources,
            "compactions": compactions,
            "tokens_estimate": estimate_tokens(messages),
        })

    return [turns[t] for t in sorted(turns)]


# ── storage codec ───────────────────────────────────────────────────────────
#
# Converts events to/from their JSONL representation. A ``surfaceOp`` is written
# as the literal ``"append"`` or a ``{"op":"replace","start","end"}`` object.


def _surface_op_to_json(op: SurfaceOp) -> Any:
    if op == APPEND:
        return "append"
    if isinstance(op, ReplaceOp):
        return {"op": "replace", "start": op.start, "end": op.end}
    raise ValueError(f"invalid surfaceOp {op!r}")


def _surface_op_from_json(value: Any) -> SurfaceOp | None:
    if value is None:
        return None
    if value == "append":
        return APPEND
    if isinstance(value, dict) and value.get("op") == "replace":
        return ReplaceOp(start=int(value["start"]), end=int(value["end"]))
    raise ValueError(f"invalid surfaceOp {value!r}")


def event_to_dict(event: SessionEvent) -> dict[str, Any]:
    """Serialize an event to its JSONL object form (lossless)."""
    d: dict[str, Any] = {
        "seq": event.seq,
        "time": event.time,
        "type": event.type,
        "data": event.data,
    }
    if event.surfaceOp is not None:
        d["surfaceOp"] = _surface_op_to_json(event.surfaceOp)
    if event.sourceEventSeqs is not None:
        d["sourceEventSeqs"] = list(event.sourceEventSeqs)
    if event.ignorable:
        d["ignorable"] = True
    return d


def event_from_dict(data: Mapping[str, Any]) -> SessionEvent:
    """Parse a JSONL event object back into a :class:`SessionEvent`."""
    return SessionEvent(
        seq=data["seq"],
        time=data["time"],
        type=data["type"],
        data=data["data"],
        surfaceOp=_surface_op_from_json(data.get("surfaceOp")),
        sourceEventSeqs=(
            tuple(data["sourceEventSeqs"])
            if data.get("sourceEventSeqs") is not None
            else None
        ),
        ignorable=bool(data.get("ignorable", False)),
    )


# ── the log ─────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


class SessionLog:
    """Append-only event log with a derived message surface.

    Create a live empty log with :meth:`create`, or reconstruct one with
    :meth:`from_events`. Every append assigns a monotonic ``seq``, validates and
    detaches its payload, applies the surface op, then notifies observers.
    """

    def __init__(self, header: SessionHeader):
        self.header = header
        self._events: list[SessionEvent] = []
        self._surface: list[SessionEvent] = []
        self._seq = 0
        self._turn_count = 0
        self._observers: list[Callable[[SessionEvent], None]] = []

    # -- construction --------------------------------------------------------

    @classmethod
    def create(cls, header: SessionHeader | Mapping[str, Any]) -> "SessionLog":
        return cls(_coerce_header(header))

    @classmethod
    def from_events(
        cls,
        header: SessionHeader | Mapping[str, Any],
        events: list[SessionEvent],
    ) -> "SessionLog":
        """Reconstruct a log from a contiguous, current-format event list."""
        log = cls(_coerce_header(header))
        for event in events:
            log._adopt(event)
        return log

    # -- read surface --------------------------------------------------------

    @property
    def id(self) -> str:
        return self.header.id

    @property
    def seq(self) -> int:
        """Next sequence number to be assigned (== current event count)."""
        return self._seq

    @property
    def turn_count(self) -> int:
        """Number of ``turn/start`` events committed so far (O(1) counter)."""
        return self._turn_count

    @property
    def events(self) -> list[SessionEvent]:
        """A snapshot of the raw log (events remain immutable)."""
        return list(self._events)

    @property
    def surface(self) -> list[SessionEvent]:
        """The ordered surface — message-producing events after ``replace`` ops."""
        return list(self._surface)

    def derive_messages(self) -> list[dict[str, Any]]:
        """Project the surface to the provider-native message list.

        Returns the ``message`` payload of each surface event in order — the exact
        messages the model saw (system prompt excluded). Messages are frozen at
        append; do not mutate the returned values.
        """
        return [event.data["message"] for event in self._surface]

    def message_sources(self) -> list[str | None]:
        """Per-surface-message source, parallel to :meth:`derive_messages`.

        ``user/message`` events yield their ``source`` (``"human"``, ``"memory"``,
        ``"skill"``, ``"mode"``, ``"compaction"``, ``"interruption"``,
        ``"loop-guard"``, ``"hook"``); ``assistant/message`` and ``tool/result``
        events yield ``None``. Compaction uses this to tell the original-task
        anchor apart from injected environment blocks.
        """
        sources: list[str | None] = []
        for event in self._surface:
            if event.type == "user/message":
                sources.append(event.data.get("source", "human"))
            else:
                sources.append(None)
        return sources

    def turn_changes(self) -> list[dict[str, Any]]:
        """Per-turn delta summary — the "what changed this turn" view.

        Returns one entry per turn with the ``request/header`` reason (plus a
        field diff against the previous header when it changed) and the synthetic
        injections (``"memory"`` / ``"skill"``) appended that turn.
        """
        result: list[dict[str, Any]] = []
        entry: dict[str, Any] | None = None
        last_header: dict[str, Any] | None = None
        for event in self._events:
            if event.type == "turn/start":
                entry = {"turn": event.data["turn"], "injections": []}
                result.append(entry)
            elif event.type == "request/header" and entry is not None:
                header = event.data["header"]
                entry["header_reason"] = event.data["reason"]
                if event.data["reason"] != "initial":
                    entry["header_diff"] = diff_headers(last_header, header)
                last_header = header
            elif event.type == "user/message" and entry is not None:
                source = event.data.get("source", "human")
                if source != "human":
                    entry["injections"].append(source)
        return result

    # -- append --------------------------------------------------------------

    def append(
        self,
        type: str,
        data: Mapping[str, Any],
        *,
        surface_op: SurfaceOp | None = None,
        source_event_seqs: list[int] | tuple[int, ...] | None = None,
        ignorable: bool = False,
    ) -> SessionEvent:
        """Append one event. Returns the committed, frozen event.

        ``surface_op`` is required for :data:`SURFACE_EVENT_TYPES` and forbidden
        otherwise. A :class:`ReplaceOp` must reference valid surface indices and
        its ``source_event_seqs`` must cover every shadowed node.
        """
        frozen = snapshot_json_value(data)
        if not isinstance(frozen, dict):
            raise ValueError(f"event data must be a JSON object, got {type(frozen).__name__}")

        is_surface = type in SURFACE_EVENT_TYPES
        if is_surface and surface_op is None:
            raise ValueError(f"surface event {type!r} requires surfaceOp")
        if not is_surface and (surface_op is not None or source_event_seqs is not None):
            raise ValueError(f"non-surface event {type!r} cannot carry surface metadata")

        if is_surface and "message" not in frozen:
            raise ValueError(f"surface event {type!r} data requires a 'message' field")

        op = self._normalize_op(surface_op) if is_surface else None
        if isinstance(op, ReplaceOp):
            self._validate_replace(op, source_event_seqs)

        event = SessionEvent(
            seq=self._seq,
            time=_now_ms(),
            type=type,
            data=frozen,
            surfaceOp=op,
            sourceEventSeqs=(
                tuple(source_event_seqs) if source_event_seqs is not None else None
            ),
            ignorable=ignorable,
        )
        self._events.append(event)
        self._apply_surface(event)
        self._seq += 1
        if event.type == "turn/start":
            self._turn_count += 1
        self._notify(event)
        return event

    def on_append(self, callback: Callable[[SessionEvent], None]) -> Callable[[], None]:
        """Register a post-commit observer. Returns a disposer."""
        self._observers.append(callback)

        def dispose() -> None:
            try:
                self._observers.remove(callback)
            except ValueError:
                pass

        return dispose

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _normalize_op(surface_op: SurfaceOp) -> SurfaceOp:
        if surface_op == APPEND:
            return APPEND
        if isinstance(surface_op, ReplaceOp):
            return surface_op
        raise ValueError(f"invalid surfaceOp {surface_op!r}")

    def _validate_replace(
        self,
        op: ReplaceOp,
        source_event_seqs: list[int] | tuple[int, ...] | None,
    ) -> None:
        n = len(self._surface)
        if op.start < 0 or op.end < op.start or op.end >= n:
            raise ValueError(
                f"replace op {op.start}:{op.end} out of range for surface of length {n}"
            )
        shadowed = {self._surface[i].seq for i in range(op.start, op.end + 1)}
        provided = set(source_event_seqs or ())
        missing = shadowed - provided
        if missing:
            raise ValueError(
                f"replace op source_event_seqs missing shadowed seqs {sorted(missing)}"
            )

    def _apply_surface(self, event: SessionEvent) -> None:
        if event.surfaceOp is None:
            return
        if event.surfaceOp == APPEND:
            self._surface.append(event)
        else:  # ReplaceOp
            op = event.surfaceOp
            self._surface[op.start : op.end + 1] = [event]

    def _adopt(self, event: SessionEvent) -> None:
        """Validate and apply an externally-built event (used by from_events)."""
        if event.seq != self._seq:
            raise ValueError(
                f"non-contiguous seq: expected {self._seq}, got {event.seq}"
            )
        if event.type in SURFACE_EVENT_TYPES:
            if event.surfaceOp is None:
                raise ValueError(f"surface event {event.type!r} missing surfaceOp")
            if "message" not in event.data:
                raise ValueError(f"surface event {event.type!r} data missing 'message'")
            if isinstance(event.surfaceOp, ReplaceOp):
                self._validate_replace(event.surfaceOp, event.sourceEventSeqs)
        elif event.surfaceOp is not None or event.sourceEventSeqs is not None:
            raise ValueError(f"non-surface event {event.type!r} carries surface metadata")
        self._events.append(event)
        self._apply_surface(event)
        self._seq += 1
        if event.type == "turn/start":
            self._turn_count += 1

    def _notify(self, event: SessionEvent) -> None:
        for callback in list(self._observers):
            try:
                callback(event)
            except Exception:
                # One observer's failure must not break the log or the others.
                _logger.warning("session observer failed", exc_info=True)


def _coerce_header(header: SessionHeader | Mapping[str, Any]) -> SessionHeader:
    if isinstance(header, SessionHeader):
        return header
    if isinstance(header, Mapping):
        return SessionHeader(**dict(header))
    raise TypeError(f"header must be SessionHeader or mapping, got {type(header).__name__}")


__all__ = [
    "SESSION_FORMAT_VERSION",
    "APPEND",
    "SURFACE_EVENT_TYPES",
    "STAGE_STREAMING",
    "STAGE_APPROVAL",
    "STAGE_TOOL_EXECUTING",
    "ReplaceOp",
    "SessionHeader",
    "SessionEvent",
    "SessionLog",
    "snapshot_json_value",
    "is_json_value",
    "canonical_header",
    "header_equals",
    "diff_headers",
    "fold_request_header",
    "reconstruct_turn_contexts",
    "event_to_dict",
    "event_from_dict",
]
