"""Tests for SkillTool (use_skill)."""

from pathlib import Path

import pytest

from cluxmate.tools.skill import SkillTool


class _FakeBuilder:
    """Stand-in for AgentBuilder: the tool reads `trusted` off it."""

    def __init__(self, tracker=None, trusted: bool = True):
        self._tracker = tracker
        self.trusted = trusted


class _FakeTracker:
    def __init__(self):
        self.calls = []

    async def on_skill_used(self, name, slug, source, trigger):
        self.calls.append((name, slug, source, trigger))


def _write_skill(root: Path, slug: str, body: str):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug.title()}\ndescription: d\n---\n{body}", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_execute_loads_and_signals(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_skill(home / ".cluxmate" / "skills", "deploy", "# Deploy\nrun it")

    tracker = _FakeTracker()
    tool = SkillTool(project_root=str(tmp_path / "proj"), builder=_FakeBuilder(tracker))
    result = await tool.execute(name="deploy")

    assert "run it" in result
    assert tracker.calls == [("Deploy", "deploy", "global", "auto")]


@pytest.mark.asyncio
async def test_execute_unknown_lists_available(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_skill(home / ".cluxmate" / "skills", "deploy", "x")

    tool = SkillTool(project_root=str(tmp_path / "proj"), builder=_FakeBuilder(None))
    result = await tool.execute(name="missing")
    assert "no skill named 'missing'" in result
    assert "deploy" in result  # lists available slugs


@pytest.mark.asyncio
async def test_risk_level_safe():
    assert SkillTool(project_root=".", builder=_FakeBuilder()).risk_level == "safe"


def _write_skill_with(root: Path, slug: str, body: str):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_project_copy_wins_and_says_what_it_shadowed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    _write_skill_with(home / ".cluxmate" / "skills", "deploy", "GLOBAL BODY")
    _write_skill_with(proj / ".cluxmate" / "skills", "deploy", "PROJECT BODY")

    tracker = _FakeTracker()
    tool = SkillTool(project_root=str(proj), builder=_FakeBuilder(tracker))
    result = await tool.execute(name="deploy")

    assert "PROJECT BODY" in result
    assert "GLOBAL BODY" not in result
    # The list showed one row, so the result must say which copy served it.
    assert "the project copy takes precedence over global:deploy" in result
    # No frontmatter in either body, so the display name falls back to the slug.
    assert tracker.calls == [("deploy", "deploy", "project", "auto")]


@pytest.mark.asyncio
async def test_no_shadow_note_when_the_slug_is_unique(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_skill(home / ".cluxmate" / "skills", "deploy", "run it")

    result = await SkillTool(project_root=str(tmp_path / "proj"), builder=_FakeBuilder()).execute(
        name="deploy"
    )
    assert result.startswith("Skill 'Deploy' loaded.")
    assert "takes precedence" not in result


@pytest.mark.asyncio
async def test_disabled_skill_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    _write_skill(home / ".cluxmate" / "skills", "deploy", "GLOBAL BODY")
    _write_skill(home / ".cluxmate" / "skills", "other", "x")
    cfg = proj / ".cluxmate"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "skills.json").write_text(
        '{"disabledSkills": ["deploy"]}', encoding="utf-8"
    )

    tracker = _FakeTracker()
    tool = SkillTool(project_root=str(proj), builder=_FakeBuilder(tracker))
    result = await tool.execute(name="deploy")

    assert result.startswith("Error: skill 'deploy' is disabled.")
    assert "GLOBAL BODY" not in result
    assert "other" in result  # still lists what IS available
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_disabling_one_copy_leaves_the_slug_loadable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    proj = tmp_path / "proj"
    _write_skill_with(home / ".cluxmate" / "skills", "deploy", "GLOBAL BODY")
    _write_skill_with(proj / ".cluxmate" / "skills", "deploy", "PROJECT BODY")
    cfg = proj / ".cluxmate"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "skills.json").write_text(
        '{"disabledSkills": ["project:deploy"]}', encoding="utf-8"
    )

    tracker = _FakeTracker()
    tool = SkillTool(project_root=str(proj), builder=_FakeBuilder(tracker))
    result = await tool.execute(name="deploy")

    assert "GLOBAL BODY" in result
    assert "takes precedence" not in result  # the shadowed copy is disabled
    assert tracker.calls == [("deploy", "deploy", "global", "auto")]
