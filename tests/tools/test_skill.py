"""Tests for SkillTool (use_skill)."""

from pathlib import Path

import pytest

from cluxmate.tools.skill import SkillTool


class _FakeBuilder:
    def __init__(self, tracker=None):
        self._tracker = tracker


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
    tool = SkillTool(cwd=str(tmp_path / "proj"), builder=_FakeBuilder(tracker))
    result = await tool.execute(name="deploy")

    assert "run it" in result
    assert tracker.calls == [("Deploy", "deploy", "global", "auto")]


@pytest.mark.asyncio
async def test_execute_unknown_lists_available(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_skill(home / ".cluxmate" / "skills", "deploy", "x")

    tool = SkillTool(cwd=str(tmp_path / "proj"), builder=_FakeBuilder(None))
    result = await tool.execute(name="missing")
    assert "no skill named 'missing'" in result
    assert "deploy" in result  # lists available slugs


@pytest.mark.asyncio
async def test_risk_level_safe():
    assert SkillTool(cwd=".", builder=_FakeBuilder()).risk_level == "safe"
