"""Tests for the read-denylist fence (read_file / grep / list_dir)."""

from pathlib import Path

import pytest

from cluxmate.tools._fence import ReadDenied, ReadFence
from cluxmate.tools.grep import GrepTool
from cluxmate.tools.list_dir import ListDirTool
from cluxmate.tools.read_file import ReadFileTool


# ---------------------------------------------------------------------------
# ReadFence unit behavior
# ---------------------------------------------------------------------------

def test_empty_fence_is_noop(tmp_path):
    fence = ReadFence()
    assert not fence.enabled
    assert fence.is_denied(tmp_path / "anything") is False
    assert fence.check(tmp_path / "anything") == (tmp_path / "anything").resolve()


def test_deny_dir_blocks_subtree(tmp_path):
    secret = tmp_path / ".ssh"
    secret.mkdir()
    fence = ReadFence(deny_paths=[str(secret)])
    assert fence.enabled
    with pytest.raises(ReadDenied):
        fence.check(secret)
    with pytest.raises(ReadDenied):
        fence.check(secret / "id_rsa")
    # Sibling is unaffected.
    assert fence.check(tmp_path / "pub.txt") == (tmp_path / "pub.txt").resolve()


def test_deny_file_blocks_only_that_file(tmp_path):
    secret_file = tmp_path / "credentials"
    secret_file.write_text("k", encoding="utf-8")
    sibling = tmp_path / "config"
    sibling.write_text("c", encoding="utf-8")
    fence = ReadFence(deny_paths=[str(secret_file)])
    with pytest.raises(ReadDenied):
        fence.check(secret_file)
    assert fence.check(sibling) == sibling.resolve()


def test_dotdot_escape_into_deny(tmp_path):
    secret = tmp_path / "secret"
    fence = ReadFence(deny_paths=[str(secret)])
    with pytest.raises(ReadDenied):
        fence.check(tmp_path / "other" / ".." / "secret" / "id_rsa")


def test_symlink_escape_resolves_into_deny(tmp_path):
    target = tmp_path / "secret"
    target.mkdir()
    (target / "id_rsa").write_text("k", encoding="utf-8")
    link_dir = tmp_path / "workspace"
    link_dir.mkdir()
    link = link_dir / "sneaky"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires privileges on this platform")
    fence = ReadFence(deny_paths=[str(target)])
    with pytest.raises(ReadDenied):
        fence.check(link / "id_rsa")


def test_error_does_not_leak_other_deny_roots(tmp_path):
    a = tmp_path / "deny-a"
    b = tmp_path / "deny-b"
    fence = ReadFence(deny_paths=[str(a), str(b)])
    with pytest.raises(ReadDenied) as exc:
        fence.check(a / "x.txt")
    assert "deny-b" not in str(exc.value)


# ---------------------------------------------------------------------------
# Built-in sensitive-file template (protect_sensitive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    ".env", ".git-credentials", ".netrc",
    "server.pem", "id_rsa.key", "cert.p12", "bundle.pfx",
    # Case-insensitive, matching the reference implementation.
    ".ENV", "Server.PEM",
])
def test_sensitive_patterns_blocked_when_enabled(tmp_path, name):
    f = tmp_path / name
    fence = ReadFence(protect_sensitive=True)
    assert fence.enabled
    assert fence.is_denied(f) is True
    with pytest.raises(ReadDenied):
        fence.check(f)


@pytest.mark.parametrize("name", [
    ".env", ".git-credentials", ".netrc",
    "server.pem", "id_rsa.key", "cert.p12", "bundle.pfx",
])
def test_sensitive_patterns_readable_by_default(tmp_path, name):
    """Zero behavior change: the template is opt-in (default off)."""
    f = tmp_path / name
    fence = ReadFence()
    assert fence.is_denied(f) is False
    assert fence.check(f) == f.resolve()


def test_sensitive_patterns_do_not_overreach(tmp_path):
    """Near-misses stay readable: .env.production is not .env, and a key
    INSIDE a filename doesn't count."""
    fence = ReadFence(protect_sensitive=True)
    assert fence.is_denied(tmp_path / ".env.production") is False
    assert fence.is_denied(tmp_path / "monkey.txt") is False
    assert fence.is_denied(tmp_path / "app.env") is False
    assert fence.check(tmp_path / "README.md") == (tmp_path / "README.md").resolve()


def test_sensitive_patterns_compose_with_deny_paths(tmp_path):
    secret = tmp_path / ".ssh"
    fence = ReadFence(deny_paths=[str(secret)], protect_sensitive=True)
    with pytest.raises(ReadDenied):
        fence.check(secret / "id_rsa")  # path root
    with pytest.raises(ReadDenied):
        fence.check(tmp_path / "creds.pem")  # pattern rule
    assert fence.check(tmp_path / "ok.txt") == (tmp_path / "ok.txt").resolve()


@pytest.mark.asyncio
async def test_grep_walk_skips_pattern_matches(tmp_path):
    (tmp_path / ".env").write_text("SECRETKEY", encoding="utf-8")
    (tmp_path / "pub.txt").write_text("HELLO", encoding="utf-8")
    tool = GrepTool(workdir=str(tmp_path), fence=ReadFence(protect_sensitive=True))
    result = await tool.execute(path=str(tmp_path), pattern="HELLO|SECRET")
    assert "pub.txt" in result
    assert ".env" not in result
    assert "SECRETKEY" not in result


# ---------------------------------------------------------------------------
# Tool integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_file_blocked(tmp_path):
    secret = tmp_path / ".ssh"
    secret.mkdir()
    f = secret / "id_rsa"
    f.write_text("PRIVATE", encoding="utf-8")
    tool = ReadFileTool(workdir=str(tmp_path), fence=ReadFence([str(secret)]))
    result = await tool.execute(path=str(f))
    assert "forbidden" in result


@pytest.mark.asyncio
async def test_read_file_allowed_without_deny(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("hello", encoding="utf-8")
    tool = ReadFileTool(workdir=str(tmp_path))
    result = await tool.execute(path=str(f))
    assert "hello" in result


@pytest.mark.asyncio
async def test_grep_single_file_blocked(tmp_path):
    secret = tmp_path / ".ssh"
    secret.mkdir()
    f = secret / "id_rsa"
    f.write_text("SECRETKEY", encoding="utf-8")
    tool = GrepTool(workdir=str(tmp_path), fence=ReadFence([str(secret)]))
    result = await tool.execute(path=str(f), pattern="SECRET")
    assert "forbidden" in result


@pytest.mark.asyncio
async def test_grep_walk_skips_denied_subtree(tmp_path):
    secret = tmp_path / ".ssh"
    secret.mkdir()
    (secret / "id_rsa").write_text("SECRETKEY", encoding="utf-8")
    (tmp_path / "pub.txt").write_text("HELLO", encoding="utf-8")
    tool = GrepTool(workdir=str(tmp_path), fence=ReadFence([str(secret)]))
    result = await tool.execute(path=str(tmp_path), pattern="HELLO|SECRET")
    assert "pub.txt" in result
    assert "id_rsa" not in result
    assert "SECRETKEY" not in result


@pytest.mark.asyncio
async def test_list_dir_blocks_denied_dir_and_hides_entries(tmp_path):
    secret = tmp_path / ".ssh"
    secret.mkdir()
    (secret / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("y", encoding="utf-8")
    tool = ListDirTool(workdir=str(tmp_path), fence=ReadFence([str(secret)]))
    blocked = await tool.execute(path=str(secret))
    assert "forbidden" in blocked
    listed = await tool.execute(path=str(tmp_path))
    assert "visible.txt" in listed
    assert ".ssh" not in listed
