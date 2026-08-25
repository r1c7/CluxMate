"""Tests for MemoryManager (global + project AGENTS.md injection)."""

from pathlib import Path

import pytest

from cluxmate.core.memory import MemoryManager, MEMORY_FILENAME, LEGACY_FILENAME


def _mgr(tmp_path: Path, monkeypatch) -> tuple[MemoryManager, Path, Path]:
    """Manager with global root redirected under tmp_path; returns (mgr, global_dir, cwd)."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return MemoryManager(str(cwd)), home / ".cluxmate", cwd


def test_empty_when_no_files(tmp_path, monkeypatch):
    mgr, _, _ = _mgr(tmp_path, monkeypatch)
    assert mgr.render() == ""


def test_project_only(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    (cwd / MEMORY_FILENAME).write_text("Use tabs, not spaces.", encoding="utf-8")
    out = mgr.render()
    assert "<project_memory>" in out
    assert "Use tabs" in out
    assert "<global_memory>" not in out


def test_global_and_project_order(tmp_path, monkeypatch):
    mgr, gdir, cwd = _mgr(tmp_path, monkeypatch)
    (gdir / MEMORY_FILENAME).write_text("GLOBAL_RULE", encoding="utf-8")
    (cwd / MEMORY_FILENAME).write_text("PROJECT_RULE", encoding="utf-8")
    out = mgr.render()
    # Global appears before project so project can override.
    assert out.index("GLOBAL_RULE") < out.index("PROJECT_RULE")
    assert "<global_memory>" in out and "<project_memory>" in out


def test_truncation(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    (cwd / MEMORY_FILENAME).write_text("x" * (40 * 1024), encoding="utf-8")
    out = mgr.render()
    assert "[memory truncated]" in out


def test_paths(tmp_path, monkeypatch):
    mgr, gdir, cwd = _mgr(tmp_path, monkeypatch)
    assert mgr.global_path() == gdir / MEMORY_FILENAME
    assert mgr.project_path() == cwd / MEMORY_FILENAME


def test_path_for_scope(tmp_path, monkeypatch):
    mgr, gdir, cwd = _mgr(tmp_path, monkeypatch)
    assert mgr.path_for("project") == cwd / MEMORY_FILENAME
    assert mgr.path_for("global") == gdir / MEMORY_FILENAME
    assert mgr.path_for() == cwd / MEMORY_FILENAME  # default is project


def test_append_creates_project_file(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    path = mgr.append("Use pytest, not unittest.")
    assert path == cwd / MEMORY_FILENAME
    assert path.is_file()
    assert "Use pytest, not unittest." in mgr.render()


def test_append_global_creates_dir(tmp_path, monkeypatch):
    # global ~/.cluxmate exists in _mgr, but append must also work if missing.
    mgr, gdir, _ = _mgr(tmp_path, monkeypatch)
    path = mgr.append("Prefer terse responses.", scope="global")
    assert path == gdir / MEMORY_FILENAME
    out = mgr.render()
    assert "<global_memory>" in out and "Prefer terse responses." in out


def test_append_separates_entries(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    mgr.append("First entry.")
    mgr.append("Second entry.")
    text = (cwd / MEMORY_FILENAME).read_text("utf-8")
    # Blank line between the two entries, both present.
    assert "First entry.\n\nSecond entry.\n" in text


def test_append_preserves_crlf(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    p = cwd / MEMORY_FILENAME
    p.write_bytes(b"Existing line.\r\n")
    mgr.append("New line.")
    raw = p.read_bytes()
    # Original CRLF style is preserved, no lone-LF introduced.
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_is_over_limit(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    assert mgr.is_over_limit() is False
    (cwd / MEMORY_FILENAME).write_text("x" * (33 * 1024), encoding="utf-8")
    assert mgr.is_over_limit() is True


def test_read_prefers_agent_over_claude(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    (cwd / MEMORY_FILENAME).write_text("AGENT_RULE", encoding="utf-8")
    (cwd / LEGACY_FILENAME).write_text("CLAUDE_RULE", encoding="utf-8")
    out = mgr.render()
    assert "AGENT_RULE" in out
    assert "CLAUDE_RULE" not in out


def test_read_falls_back_to_claude(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    (cwd / LEGACY_FILENAME).write_text("CLAUDE_RULE", encoding="utf-8")
    out = mgr.render()
    assert "CLAUDE_RULE" in out


def test_read_falls_back_to_claude_global(tmp_path, monkeypatch):
    mgr, gdir, _ = _mgr(tmp_path, monkeypatch)
    home = gdir.parent
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / LEGACY_FILENAME).write_text("GLOBAL_CLAUDE_RULE", encoding="utf-8")
    out = mgr.render()
    assert "<global_memory>" in out
    assert "GLOBAL_CLAUDE_RULE" in out


def test_append_targets_agent_only(tmp_path, monkeypatch):
    mgr, _, cwd = _mgr(tmp_path, monkeypatch)
    legacy = cwd / LEGACY_FILENAME
    legacy.write_text("Existing Claude rule.", encoding="utf-8")
    mgr.append("New agent rule.")
    assert (cwd / MEMORY_FILENAME).is_file()
    assert "New agent rule." in (cwd / MEMORY_FILENAME).read_text("utf-8")
    # Legacy file is left untouched.
    assert legacy.read_text("utf-8") == "Existing Claude rule."
