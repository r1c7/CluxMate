"""Tests for SkillManager (skill discovery for the agent)."""

from pathlib import Path

import pytest

from cluxmate.core.skills import SkillManager, _parse_frontmatter


def _write_skill(root: Path, slug: str, frontmatter: str | None, body: str = "body"):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ""
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")


def _mgr(tmp_path: Path, monkeypatch) -> tuple[SkillManager, Path]:
    """A manager whose global root is redirected under tmp_path."""
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return SkillManager(str(cwd)), tmp_path


def test_parse_frontmatter_quotes():
    md = '---\nname: "Deploy Helper"\ndescription: \'Ship it\'\n---\n# hi'
    fm = _parse_frontmatter(md)
    assert fm["name"] == "Deploy Helper"
    assert fm["description"] == "Ship it"


def test_parse_frontmatter_none():
    assert _parse_frontmatter("# no frontmatter\ntext") == {}


def test_discover_global_and_project(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    gskills = base / "home" / ".cluxmate" / "skills"
    pskills = base / "proj" / ".cluxmate" / "skills"
    _write_skill(gskills, "pdf", "name: PDF Tools\ndescription: Work with PDFs")
    _write_skill(gskills, "bare", None)  # no frontmatter → name falls back to slug
    _write_skill(pskills, "deploy", "name: Deploy Helper\ndescription: Ship it")

    skills = mgr.discover()
    by_slug = {s.slug: s for s in skills}
    assert by_slug["pdf"].name == "PDF Tools"
    assert by_slug["pdf"].source == "global"
    assert by_slug["bare"].name == "bare"  # fallback
    assert by_slug["deploy"].name == "Deploy Helper"
    assert by_slug["deploy"].source == "project"


def test_dir_without_skill_md_ignored(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    gskills = base / "home" / ".cluxmate" / "skills"
    (gskills / "not-a-skill").mkdir()
    (gskills / "not-a-skill" / "README.md").write_text("x", encoding="utf-8")
    assert mgr.discover() == []


def test_get_by_slug(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "deploy",
                 "name: Deploy Helper\ndescription: d")
    assert mgr.get("deploy").name == "Deploy Helper"  # by slug, not display name
    assert mgr.get("Deploy Helper") is None
    assert mgr.get("nope") is None


def test_read_returns_content(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "deploy",
                 "name: D\ndescription: d", body="# Deploy\nstep 1")
    content = mgr.read("deploy")
    assert "step 1" in content
    assert mgr.read("nope") is None
