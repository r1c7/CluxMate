"""Tests for the project-scoped tool-approval policy.

Safety-critical: 'dangerous' must never auto-approve under accept_edits or the
write-tier always-allow list; only an explicit dangerous-tier grant (delete_file
/ bash) or yolo green-lights it, and sandbox escalation never auto-approves.
Scope-critical: policy is per-cwd and must NOT leak across working directories."""

import json

import pytest

from cluxmate.core.permissions import PermissionPolicy, PermissionStore


def test_safe_always_auto_approved(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    assert p.is_auto_approved("read_file", "safe") is True


def test_write_prompts_by_default(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    assert p.is_auto_approved("write_file", "write") is False


def test_write_auto_approved_under_accept_edits(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_accept_edits(True)
    assert p.is_auto_approved("write_file", "write") is True


def test_dangerous_never_auto_approved_under_accept_edits(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_accept_edits(True)
    # accept_edits covers writes only — a dangerous bash/delete still prompts.
    assert p.is_auto_approved("bash", "dangerous") is False
    assert p.is_auto_approved("delete_file", "dangerous") is False


def test_always_allow_auto_approves_named_write_tool(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow("search_replace")
    assert p.is_auto_approved("search_replace", "write") is True
    assert p.is_auto_approved("write_file", "write") is False


def test_always_allow_does_not_cover_dangerous(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow("bash")
    # "always approve bash" at the WRITE tier must not green-light `rm -rf` —
    # dangerous needs its own tier.
    assert p.is_auto_approved("bash", "dangerous") is False


def test_always_allow_dangerous_covers_delete_file(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("delete_file")
    assert p.is_auto_approved("delete_file", "dangerous") is True
    # The dangerous grant is delete-specific: it must not leak to other tools.
    assert p.is_auto_approved("bash", "dangerous") is False


def test_bare_bash_grant_is_rejected_and_inert(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    # A bare "bash" is not a valid dangerous grant — only bash:<category> works.
    p.add_always_allow_dangerous("bash")
    assert p.snapshot()["always_allow_dangerous_tools"] == []
    assert p.is_auto_approved("bash", "dangerous", categories=frozenset({"rm"})) is False
    # The dangerous tier is separate from the write tier.
    assert p.is_auto_approved("bash", "write") is False


def test_bash_dangerous_category_scoped(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("bash:rm")
    # Only the granted category auto-approves.
    assert p.is_auto_approved("bash", "dangerous", categories=frozenset({"rm"})) is True
    assert p.is_auto_approved("bash", "dangerous", categories=frozenset({"git-reset-hard"})) is False
    # A multi-category command needs EVERY category granted.
    assert p.is_auto_approved("bash", "dangerous", categories=frozenset({"rm", "git-reset-hard"})) is False
    p.add_always_allow_dangerous("bash:git-reset-hard")
    assert p.is_auto_approved("bash", "dangerous", categories=frozenset({"rm", "git-reset-hard"})) is True


def test_add_always_allow_dangerous_accepts_bash_category(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("bash:rm")
    assert p.snapshot()["always_allow_dangerous_tools"] == ["bash:rm"]


def test_escalation_never_auto_approved_even_when_dangerous_allowed(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("bash:rm")
    p.add_always_allow_dangerous("delete_file")
    # danger-full-access bypasses the sandbox/fence: always prompts, regardless
    # of the dangerous-tier grant.
    assert p.is_auto_approved("bash", "dangerous", escalated=True, categories=frozenset({"rm"})) is False
    assert p.is_auto_approved("delete_file", "dangerous", escalated=True) is False
    # yolo still disarms everything, escalation included.
    p.set_mode("yolo")
    assert p.is_auto_approved("bash", "dangerous", escalated=True) is True


def test_critical_never_auto_approved(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("bash")
    # Device/system-level destruction (format/mkfs/dd/…) is never auto-approved,
    # even when bash's dangerous tier is always-allowed.
    assert p.is_auto_approved("bash", "critical") is False
    assert p.is_auto_approved("bash", "critical", escalated=True) is False


def test_critical_never_always_allowable(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    assert p.is_always_allowable("bash", "critical") is False
    assert p.is_always_allowable("bash", "critical", escalated=True) is False


def test_yolo_auto_approves_critical(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_mode("yolo")
    assert p.is_auto_approved("bash", "critical") is True


def test_add_always_allow_dangerous_rejects_unknown_tool(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow_dangerous("some_mcp_dangerous_tool")
    assert p.is_auto_approved("some_mcp_dangerous_tool", "dangerous") is False
    assert p.snapshot()["always_allow_dangerous_tools"] == []


def test_is_always_allowable_matrix(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    assert p.is_always_allowable("write_file", "write") is True
    assert p.is_always_allowable("bash", "write") is True
    assert p.is_always_allowable("delete_file", "dangerous") is True
    assert p.is_always_allowable("bash", "dangerous") is True
    # Escalation is never always-allowable.
    assert p.is_always_allowable("bash", "dangerous", escalated=True) is False
    assert p.is_always_allowable("write_file", "write", escalated=True) is False
    # Non-allowable dangerous tools (e.g. MCP) and safe tools → no button.
    assert p.is_always_allowable("some_mcp_tool", "dangerous") is False
    assert p.is_always_allowable("read_file", "safe") is False
    # Critical (device/system-level destruction) → never a button.
    assert p.is_always_allowable("bash", "critical") is False


def test_always_allow_dangerous_persists_across_reload(tmp_path):
    p1 = PermissionPolicy(str(tmp_path))
    p1.add_always_allow_dangerous("bash:rm")
    p1.add_always_allow_dangerous("delete_file")

    p2 = PermissionPolicy(str(tmp_path))
    assert p2.is_auto_approved("bash", "dangerous", categories=frozenset({"rm"})) is True
    assert p2.is_auto_approved("delete_file", "dangerous") is True
    assert p2.mode == "default"  # mode still resets; the grant persists.


def test_only_always_allow_persists_not_mode(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_mode("yolo")
    p.add_always_allow("write_file")
    # always_allow persists; mode is per-session and must NOT be written.
    f = tmp_path / ".cluxmate" / "permissions.json"
    assert f.exists()
    data = json.loads(f.read_text("utf-8"))
    assert "accept_edits" not in data
    assert "mode" not in data
    assert "write_file" in data["always_allow_tools"]
    assert data["always_allow_dangerous_tools"] == []


def test_mode_not_persisted_across_reload(tmp_path):
    p1 = PermissionPolicy(str(tmp_path))
    p1.set_mode("yolo")
    p1.add_always_allow("write_file")
    # A fresh policy (re-initialize) reloads always_allow but resets mode to default.
    p2 = PermissionPolicy(str(tmp_path))
    assert p2.mode == "default"
    assert p2.is_auto_approved("write_file", "write") is True  # from always_allow


def test_always_allow_is_per_project(tmp_path):
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    a = PermissionPolicy(str(proj_a))
    a.add_always_allow("write_file")

    # Switching to a different working dir must NOT inherit project A's always-allow.
    b = PermissionPolicy(str(proj_b))
    assert b.is_auto_approved("write_file", "write") is False


def test_add_always_allow_dedups(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.add_always_allow("write_file")
    p.add_always_allow("write_file")
    assert p.snapshot()["always_allow_tools"] == ["write_file"]


def test_store_handles_corrupt_file(tmp_path):
    d = tmp_path / ".cluxmate"
    d.mkdir()
    (d / "permissions.json").write_text("{not json", "utf-8")
    # Corrupt file → defaults, no crash.
    state = PermissionStore(str(tmp_path)).load()
    assert state == {"always_allow_tools": [], "always_allow_dangerous_tools": []}


# ── development-mode truth table ──────────────────────────────────────────
# safe always auto-approves; the interesting axes are write × dangerous per mode.

def test_yolo_auto_approves_everything(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_mode("yolo")
    assert p.is_auto_approved("read_file", "safe") is True
    assert p.is_auto_approved("write_file", "write") is True
    # The whole point of yolo: dangerous auto-approves too (rm -rf, delete_file).
    assert p.is_auto_approved("bash", "dangerous") is True
    assert p.is_auto_approved("delete_file", "dangerous") is True


def test_accept_edits_mode_auto_approves_write_not_dangerous(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_mode("acceptEdits")
    assert p.is_auto_approved("write_file", "write") is True
    assert p.is_auto_approved("bash", "dangerous") is False


def test_default_mode_prompts_write_and_dangerous(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    assert p.mode == "default"
    assert p.is_auto_approved("write_file", "write") is False
    assert p.is_auto_approved("bash", "dangerous") is False


def test_set_accept_edits_shim_maps_to_mode(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    p.set_accept_edits(True)
    assert p.mode == "acceptEdits"
    p.set_accept_edits(False)
    assert p.mode == "default"
    # Turning the old boolean off must not silently downgrade yolo.
    p.set_mode("yolo")
    p.set_accept_edits(False)
    assert p.mode == "yolo"


def test_invalid_mode_rejected(tmp_path):
    p = PermissionPolicy(str(tmp_path))
    with pytest.raises(ValueError):
        p.set_mode("bogus")


# ── subagent gating (ScopedCallbacks) ─────────────────────────────────────
# Subagents run autonomously (no interactive prompt), but "spawn a subagent"
# must not bypass dangerous-command gating. Rule: safe+write always run;
# dangerous runs only in yolo.

@pytest.mark.asyncio
async def test_subagent_dangerous_denied_outside_yolo(tmp_path):
    from cluxmate.core.jsonrpc_server import JsonRpcCallbacks

    policy = PermissionPolicy(str(tmp_path))  # default mode
    shared = JsonRpcCallbacks(policy)
    scoped = shared.scoped("child-1")

    # safe + write autonomously run even in default mode.
    assert (await scoped.on_tool_start("read_file", {}, "c1", "safe")).approved is True
    assert (await scoped.on_tool_start("write_file", {}, "c2", "write")).approved is True
    # dangerous is denied (no prompt) outside yolo.
    assert (await scoped.on_tool_start("bash", {"command": "rm -rf /"}, "c3", "dangerous")).approved is False

    policy.set_mode("acceptEdits")
    assert (await scoped.on_tool_start("delete_file", {}, "c4", "dangerous")).approved is False


@pytest.mark.asyncio
async def test_subagent_dangerous_allowed_in_yolo(tmp_path):
    from cluxmate.core.jsonrpc_server import JsonRpcCallbacks

    policy = PermissionPolicy(str(tmp_path))
    policy.set_mode("yolo")
    shared = JsonRpcCallbacks(policy)
    scoped = shared.scoped("child-1")

    # yolo: subagent dangerous runs, matching the root yolo semantics.
    assert (await scoped.on_tool_start("bash", {"command": "rm -rf x"}, "c1", "dangerous")).approved is True
    assert (await scoped.on_tool_start("delete_file", {}, "c2", "dangerous")).approved is True
