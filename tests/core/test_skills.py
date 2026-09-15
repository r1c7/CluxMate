"""Tests for SkillManager (skill discovery for the agent)."""

import json
from pathlib import Path

import pytest

from cluxmate.core.builder import AgentBuilder
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


def _disable(cwd: Path, *ids: str):
    """Write <cwd>/.cluxmate/skills.json with these disabledSkills entries."""
    cfg_dir = cwd / ".cluxmate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "skills.json").write_text(
        json.dumps({"disabledSkills": list(ids)}), encoding="utf-8"
    )


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


# --- same slug in both roots (global vs project) ----------------------------


def test_same_slug_project_shadows_global(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf",
                 "name: Global PDF\ndescription: global one", body="global body")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "pdf",
                 "name: Project PDF\ndescription: project one", body="project body")

    # discover() keeps both copies so the desktop can list/toggle each one.
    both = mgr.discover()
    assert [s.id for s in both] == ["global:pdf", "project:pdf"]
    by_id = {s.id: s for s in both}
    assert by_id["global:pdf"].shadowed is True
    assert by_id["project:pdf"].shadowed is False

    # The agent-visible surface has ONE row for the slug, the project copy.
    enabled = mgr.discover_enabled()
    assert [s.slug for s in enabled] == ["pdf"]
    assert enabled[0].id == "project:pdf"
    assert enabled[0].description == "project one"

    assert mgr.get("pdf").id == "project:pdf"
    assert "project body" in mgr.read("pdf")
    assert [s.id for s in mgr.overridden_copies("pdf")] == ["global:pdf"]


def test_no_collision_marks_nothing_shadowed(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf", "description: g")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "deploy", "description: p")

    assert all(not s.shadowed for s in mgr.discover())
    assert [s.slug for s in mgr.discover_enabled()] == ["deploy", "pdf"]
    assert mgr.overridden_copies("pdf") == []


def test_enabled_list_is_sorted_for_cache_stability(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    for slug in ("zeta", "alpha"):
        _write_skill(base / "home" / ".cluxmate" / "skills", slug, "description: g")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "mid", "description: p")
    # Global group first is discover()'s order; the enabled set is one sorted
    # list so a root appearing/disappearing doesn't reshuffle the prefix.
    assert [s.slug for s in mgr.discover_enabled()] == ["alpha", "mid", "zeta"]


def test_injection_block_lists_a_colliding_slug_once(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf",
                 "name: Global PDF\ndescription: global one")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "pdf",
                 "name: Project PDF\ndescription: project one")

    builder = AgentBuilder(str(base / "proj"), object())
    blocks = [text for source, text in builder.render_injections() if source == "skill"]
    assert len(blocks) == 1
    assert blocks[0].count("**pdf**") == 1
    assert "project one" in blocks[0]
    assert "global one" not in blocks[0]


# --- disabling, per slug or per copy ---------------------------------------


def test_bare_slug_disables_every_copy(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf", "description: g")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "pdf", "description: p")
    _disable(base / "proj", "pdf")

    assert mgr.discover_enabled() == []
    assert mgr.get("pdf") is None  # no enabled copy left
    assert all(s.disabled for s in mgr.discover())


def test_disabling_global_copy_falls_back_to_project_copy(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf",
                 "description: g", body="global body")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "pdf",
                 "description: p", body="project body")
    _disable(base / "proj", "global:pdf")

    assert [s.id for s in mgr.discover_enabled()] == ["project:pdf"]
    assert mgr.get("pdf").id == "project:pdf"


def test_disabling_project_copy_falls_back_to_global_copy(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf",
                 "description: g", body="global body")
    _write_skill(base / "proj" / ".cluxmate" / "skills", "pdf",
                 "description: p", body="project body")
    _disable(base / "proj", "project:pdf")

    assert [s.id for s in mgr.discover_enabled()] == ["global:pdf"]
    assert mgr.get("pdf").id == "global:pdf"
    assert "global body" in mgr.read("pdf")
    # The global copy now SERVES the slug, so it is not a shadowed loser.
    assert {s.id: s.shadowed for s in mgr.discover()} == {
        "global:pdf": False, "project:pdf": False,
    }
    assert mgr.overridden_copies("pdf") == []


def test_qualified_entry_is_inert_without_that_copy(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "solo", "description: g")
    _disable(base / "proj", "project:solo")  # no project copy exists

    assert [s.id for s in mgr.discover_enabled()] == ["global:solo"]
    assert mgr.get("solo") is not None


def test_bare_and_qualified_entries_coexist(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    g = base / "home" / ".cluxmate" / "skills"
    p = base / "proj" / ".cluxmate" / "skills"
    _write_skill(g, "pdf", "description: g")
    _write_skill(p, "pdf", "description: p")
    _write_skill(g, "other", "description: o")
    _disable(base / "proj", "global:pdf", "other")

    assert [s.id for s in mgr.discover_enabled()] == ["project:pdf"]
    assert mgr.get("other") is None


def test_malformed_skills_json_disables_nothing(tmp_path, monkeypatch):
    mgr, base = _mgr(tmp_path, monkeypatch)
    _write_skill(base / "home" / ".cluxmate" / "skills", "pdf", "description: g")
    cfg = base / "proj" / ".cluxmate"
    cfg.mkdir(parents=True, exist_ok=True)

    # Every shape a hand-edited file can hold: unparsable, and valid JSON that
    # is not an object (discover() runs on every turn's injection, so none of
    # these may raise).
    for body in ("{not json", "[]", "null", '"pdf"', "3", '{"disabledSkills": 7}'):
        (cfg / "skills.json").write_text(body, encoding="utf-8")
        assert [s.slug for s in mgr.discover_enabled()] == ["pdf"]
        assert mgr.get("pdf") is not None
