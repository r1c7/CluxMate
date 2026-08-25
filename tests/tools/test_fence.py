"""Tests for the write fence (sandbox phase 0 — canonicalize-then-contain)."""

import tempfile
from pathlib import Path

import pytest

from cluxmate.tools._fence import SandboxViolation, WriteFence
from cluxmate.tools.delete_file import DeleteFileTool
from cluxmate.tools.multi_edit import MultiEditTool
from cluxmate.tools.multi_write import MultiWriteTool
from cluxmate.tools.search_replace import SearchReplaceTool
from cluxmate.tools.write_file import WriteFileTool


# ---------------------------------------------------------------------------
# WriteFence unit behavior
# ---------------------------------------------------------------------------

def test_roots_are_workdir_and_tempdir(tmp_path):
    fence = WriteFence(str(tmp_path))
    roots = fence.roots()
    assert tmp_path.resolve() in roots
    assert Path(tempfile.gettempdir()).resolve() in roots


def test_check_allows_inside_workspace(tmp_path):
    fence = WriteFence(str(tmp_path))
    resolved = fence.check(tmp_path / "sub" / "f.txt")
    assert resolved == (tmp_path / "sub" / "f.txt").resolve()


def test_check_rejects_outside_workspace(tmp_path):
    # NOTE: tmp_path itself lives under the system temp root (a writable
    # root), so paths derived from it can't test escape. Build a workdir
    # in the repo tree instead — the repo is NOT under temp.
    repo = Path(__file__).resolve().parent.parent.parent
    workdir = repo / ".pytest-scratch" / "fence-outside"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        fence = WriteFence(str(workdir))
        with pytest.raises(SandboxViolation):
            fence.check(Path.home() / ".cluxmate-fence-should-not-exist.txt")
    finally:
        workdir.rmdir()


def test_check_rejects_dotdot_escape(tmp_path):
    repo = Path(__file__).resolve().parent.parent.parent
    workdir = repo / ".pytest-scratch" / "fence-dotdot"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        fence = WriteFence(str(workdir))
        with pytest.raises(SandboxViolation):
            # repo/.pytest-scratch/fence-dotdot/../../../../etc → escapes.
            fence.check(workdir / ".." / ".." / ".." / ".." / "etc" / "passwd")
    finally:
        workdir.rmdir()


def test_check_resolves_symlink_escape(tmp_path):
    # A symlink inside the workspace pointing outside must be rejected:
    # resolve() follows links BEFORE the containment comparison.
    # Creating symlinks on Windows needs admin/dev-mode privileges — skip
    # when the platform refuses.
    repo = Path(__file__).resolve().parent.parent.parent
    workdir = repo / ".pytest-scratch" / "fence-symlink"
    workdir.mkdir(parents=True, exist_ok=True)
    target = repo / ".pytest-scratch" / "symlink-target.txt"
    link = workdir / "sneaky.txt"
    # The target sits OUTSIDE workdir but the link sits inside — resolving
    # the link must land on the target, which escapes the fence.
    target.write_text("x", encoding="utf-8")
    try:
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation requires privileges on this platform")
        # workdir is the ONLY root here relative to the scratch tree; the
        # resolved target (scratch/symlink-target.txt) is outside it.
        with pytest.raises(SandboxViolation):
            WriteFence(str(workdir)).check(link)
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        workdir.rmdir()


def test_disabled_fence_passes_everything(tmp_path):
    fence = WriteFence(str(tmp_path), enabled=False)
    outside = Path("C:/Windows/System32/whatever.txt") if Path("C:/").exists() \
        else Path("/etc/passwd")
    resolved = fence.check(outside)  # no raise; returned as-is (unresolved)
    assert resolved == outside


def test_check_message_returns_empty_when_allowed(tmp_path):
    fence = WriteFence(str(tmp_path))
    assert fence.check_message(tmp_path / "ok.txt") == ""


def test_check_message_returns_error_when_violated():
    repo = Path(__file__).resolve().parent.parent.parent
    workdir = repo / ".pytest-scratch" / "fence-message"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        msg = WriteFence(str(workdir)).check_message(
            workdir / ".." / ".." / ".." / ".." / "x.txt"
        )
        assert "outside the writable sandbox" in msg
    finally:
        workdir.rmdir()


# ---------------------------------------------------------------------------
# Deny subtree (<cwd>/.cluxmate) + global memory file whitelist
# ---------------------------------------------------------------------------

def test_deny_cluxmate_state_dir_inside_workspace(tmp_path):
    # <cwd>/.cluxmate sits INSIDE the workspace root but must be denied:
    # it holds permissions.json / mcp.json — a model must not edit its own
    # permission config.
    fence = WriteFence(str(tmp_path))
    with pytest.raises(SandboxViolation):
        fence.check(tmp_path / ".cluxmate" / "permissions.json")
    msg = fence.check_message(tmp_path / ".cluxmate" / "mcp.json")
    assert "protected directory" in msg


def test_deny_wins_over_yolo_whitelisting(tmp_path):
    # The deny subtree is checked first: even a path that would otherwise be
    # inside a writable root (it IS — same tmp tree) is rejected.
    fence = WriteFence(str(tmp_path))
    assert fence.check_message(tmp_path / "plain.txt") == ""
    assert "protected" in fence.check_message(tmp_path / ".cluxmate" / "x")


def test_global_memory_file_whitelisted(tmp_path):
    # Exactly ~/.cluxmate/AGENTS.md is writable (update_memory's contract:
    # global entries are corrected via search_replace), NOT the directory.
    fence = WriteFence(str(tmp_path))
    mem = Path.home() / ".cluxmate" / "AGENTS.md"
    assert fence.check(mem) == mem.resolve()
    # The containing directory is NOT whitelisted...
    with pytest.raises(SandboxViolation):
        fence.check(Path.home() / ".cluxmate" / "config.json")
    with pytest.raises(SandboxViolation):
        fence.check(Path.home() / ".cluxmate" / "sessions" / "x.jsonl")


@pytest.mark.asyncio
async def test_write_file_blocked_in_cluxmate_dir(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(
        path=str(tmp_path / ".cluxmate" / "permissions.json"),
        content='{"always_allow_tools": ["bash"]}',
    )
    assert "protected directory" in result
    assert not (tmp_path / ".cluxmate" / "permissions.json").exists()


@pytest.mark.asyncio
async def test_search_replace_allows_global_memory(tmp_path):
    # The documented contract: "to correct a global entry, edit AGENTS.md
    # with search_replace" — must actually work through the fence. Append a
    # unique marker line, replace it, then remove it: existing user content
    # is preserved byte-for-byte either way.
    mem = Path.home() / ".cluxmate" / "AGENTS.md"
    marker = "fence-test-marker-line-9f3a"
    existed = mem.exists()
    old_bytes = mem.read_bytes() if existed else b""
    try:
        mem.parent.mkdir(parents=True, exist_ok=True)
        with mem.open("ab") as f:
            f.write(f"\n{marker}\n".encode("utf-8"))
        tool = SearchReplaceTool(workdir=str(tmp_path))
        result = await tool.execute(
            path=str(mem), old_string=marker, new_string=marker + "-v2"
        )
        assert "Replaced 1 occurrence" in result
        assert (marker + "-v2") in mem.read_text(encoding="utf-8")
    finally:
        if existed:
            mem.write_bytes(old_bytes)
        elif mem.exists():
            mem.unlink()


# ---------------------------------------------------------------------------
# Granted folders (sandbox-grants.json)
# ---------------------------------------------------------------------------

def test_granted_folder_is_writable(tmp_path):
    import shutil
    granted = Path(tempfile.mkdtemp(prefix="cluxmate-grant-"))
    try:
        fence = WriteFence(str(tmp_path), grant_paths=[str(granted)])
        # Inside the grant → allowed (it's outside the cwd + temp roots).
        resolved = fence.check(granted / "out.txt")
        assert resolved == (granted / "out.txt").resolve()
        # Outside the grant and cwd → still denied. (granted.parent is the
        # system temp dir — itself a writable root — so use home instead.)
        other = Path.home() / "cluxmate-grant-outside-test.txt"
        with pytest.raises(SandboxViolation):
            fence.check(other)
    finally:
        shutil.rmtree(granted, ignore_errors=True)


def test_granted_folder_deny_subtree_still_holds(tmp_path):
    import shutil
    granted = Path(tempfile.mkdtemp(prefix="cluxmate-grant-"))
    try:
        fence = WriteFence(str(tmp_path), grant_paths=[str(granted)])
        assert "protected" in fence.check_message(tmp_path / ".cluxmate" / "x")
    finally:
        shutil.rmtree(granted, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tool integration: each write/delete surface enforces the fence
# ---------------------------------------------------------------------------

_OUTSIDE = Path("C:/Windows/Temp/cluxmate-fence-test.txt") \
    if Path("C:/").exists() else Path("/tmp/../etc/cluxmate-fence-test.txt")


@pytest.mark.asyncio
async def test_write_file_blocked_outside(tmp_path):
    tool = WriteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path=str(_OUTSIDE), content="x")
    assert "outside the writable sandbox" in result
    assert not _OUTSIDE.exists()


@pytest.mark.asyncio
async def test_delete_file_blocked_outside(tmp_path):
    tool = DeleteFileTool(workdir=str(tmp_path))
    result = await tool.execute(path=str(_OUTSIDE))
    assert "outside the writable sandbox" in result


@pytest.mark.asyncio
async def test_search_replace_blocked_outside(tmp_path):
    tool = SearchReplaceTool(workdir=str(tmp_path))
    result = await tool.execute(path=str(_OUTSIDE), old_string="a", new_string="b")
    assert "outside the writable sandbox" in result


@pytest.mark.asyncio
async def test_multi_edit_blocked_outside(tmp_path):
    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(edits=[
        {"path": str(_OUTSIDE), "old_string": "a", "new_string": "b"},
    ])
    assert "outside the writable sandbox" in result
    assert "0/1" in result


@pytest.mark.asyncio
async def test_multi_write_blocked_outside(tmp_path):
    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(files=[
        {"path": str(_OUTSIDE), "content": "x"},
    ])
    assert "outside the writable sandbox" in result
    assert "0/1" in result


@pytest.mark.asyncio
async def test_yolo_fence_disabled_allows_outside(tmp_path):
    # yolo mode constructs tools with a disabled fence: writes go through.
    fence = WriteFence(str(tmp_path), enabled=False)
    outside_dir = Path(tempfile.mkdtemp())
    target = outside_dir / "yolo.txt"
    # The default fence would ALLOW tempdir writes anyway; prove the disabled
    # fence doesn't raise on a would-be-forbidden sibling path instead.
    forbidden = tmp_path.parent.parent / "yolo-fence-test.txt"
    try:
        fence.check(forbidden)  # no raise
        assert not target.parent == forbidden  # sanity
    finally:
        pass
    # And a tool wired with the disabled fence writes wherever asked:
    tool = WriteFileTool(workdir=str(tmp_path), fence=fence)
    result = await tool.execute(path=str(target), content="yolo")
    assert "Created" in result
    assert target.read_text(encoding="utf-8") == "yolo"
    target.unlink()
