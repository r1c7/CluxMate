"""Tests for the subagent type registry (core/subagents.py)."""

from pathlib import Path

import pytest

from cluxmate.core.subagents import (
    BUILTIN_AGENT_TYPES,
    DEFAULT_SUBAGENT_MAX_TURNS,
    EDIT_TOOL_NAMES,
    MAX_AGENT_TURNS,
    SubagentRegistry,
)


def _write_agent(root: Path, slug: str, body: str) -> None:
    d = root / ".cluxmate" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(body, encoding="utf-8")


def _reg(tmp_path: Path, home: Path | None = None) -> SubagentRegistry:
    home = home or (tmp_path / "home")
    home.mkdir(parents=True, exist_ok=True)
    return SubagentRegistry(str(tmp_path), model_ids={"deepseek-v4-fast"})


def test_no_config_yields_builtins_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    reg = _reg(tmp_path, home)
    assert list(reg.types()) == ["general-purpose", "explore", "reviewer"]
    assert reg.errors() == []
    assert reg.types()["general-purpose"].max_turns == 150
    assert reg.types()["explore"].readonly is True
    assert reg.types()["explore"].subagents_mode == "list"


def test_project_definition_parsed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_agent(tmp_path, "code-reviewer", (
        "---\n"
        "name: 代码审查员\n"
        "description: 审查一处改动是否引入安全问题\n"
        "tools: [read_file, grep, lsp]\n"
        "model: deepseek-v4-fast\n"
        "max_turns: 30\n"
        "---\n"
        "只报告真实缺陷，每条给出 文件:行号。\n"
    ))
    t = _reg(tmp_path, home).types()["code-reviewer"]
    assert t.name == "代码审查员"
    assert t.tools == ("read_file", "grep", "lsp")
    assert t.model == "deepseek-v4-fast"
    assert t.max_turns == 30
    assert t.readonly is True
    assert t.source == "project"
    assert t.builtin is False
    assert "只报告真实缺陷" in t.instructions


def test_block_list_tools_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "writer", (
        "---\n"
        "description: 写文件\n"
        "tools:\n"
        "  - read_file\n"
        "  - write_file\n"
        "subagents:\n"
        "  - explore\n"
        "---\n"
    ))
    t = _reg(tmp_path).types()["writer"]
    assert t.tools == ("read_file", "write_file")
    assert t.subagents_mode == "list"
    assert t.subagents == ("explore",)
    assert t.readonly is False


def test_project_overrides_global_and_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cluxmate" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / ".cluxmate" / "agents" / "explore.md").write_text(
        "---\ndescription: global explore\ntools: [read_file]\n---\n", encoding="utf-8"
    )
    _write_agent(tmp_path, "explore", "---\ndescription: project explore\n---\n")
    types = _reg(tmp_path, home).types()
    assert types["explore"].description == "project explore"
    assert types["explore"].source == "project"


@pytest.mark.parametrize(
    "body,expect_err",
    [
        ("---\ntools: [read_file]\n---\n", "description"),          # 缺 description
        ("---\ndescription: x\ntools: [nope]\n---\n", "nope"),      # 未知工具
        ("---\ndescription: x\ntools: [todo_write]\n---\n", "todo_write"),  # 父级专属
        ("---\ndescription: x\nmodel: no-such-model\n---\n", "no-such-model"),
        ("---\ndescription: x\nmax_turns: 0\n---\n", "max_turns"),
        ("---\ndescription: x\nsubagents: [ghost]\n---\n", "ghost"),
    ],
)
def test_invalid_definitions_are_dropped_with_reason(tmp_path, monkeypatch, body, expect_err):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "broken", body)
    reg = _reg(tmp_path)
    assert "broken" not in reg.types()
    assert any(expect_err in e["error"] for e in reg.errors())


def test_bad_slug_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "Bad_Slug", "---\ndescription: x\n---\n")
    reg = _reg(tmp_path)
    assert "Bad_Slug" not in reg.types()
    assert reg.errors()[0]["path"].endswith("Bad_Slug.md")


def test_max_turns_over_cap_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "big", "---\ndescription: x\nmax_turns: 9000\n---\n")
    assert _reg(tmp_path).types()["big"].max_turns == 150


def test_default_tools_are_read_only_without_task(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "plain", "---\ndescription: x\n---\n")
    t = _reg(tmp_path).types()["plain"]
    assert "task" not in t.tools
    assert t.readonly is True
    assert t.max_turns == DEFAULT_SUBAGENT_MAX_TURNS


def test_order_is_deterministic_and_builtins_first(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    for slug in ("zeta", "alpha", "mid"):
        _write_agent(tmp_path, slug, "---\ndescription: x\n---\n")
    reg = _reg(tmp_path)
    assert list(reg.types()) == [
        "general-purpose", "explore", "reviewer", "alpha", "mid", "zeta"
    ]


def test_claude_agents_dir_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "sneaky.md").write_text("---\ndescription: x\ntools: [bash]\n---\n", encoding="utf-8")
    assert "sneaky" not in _reg(tmp_path).types()


def test_forward_reference_across_sort_order_loads(tmp_path, monkeypatch):
    """alpha.md referencing zeta.md works regardless of filename sort order."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    _write_agent(tmp_path, "zeta", "---\ndescription: z\n---\n")
    _write_agent(tmp_path, "alpha", (
        "---\ndescription: a\ntools: [read_file, task]\nsubagents: [zeta]\n---\n"
    ))
    types = _reg(tmp_path).types()
    assert "alpha" in types and "zeta" in types
    assert types["alpha"].subagents_mode == "list"
    assert types["alpha"].subagents == ("zeta",)


def test_global_definition_can_reference_project_slug(tmp_path, monkeypatch):
    """A global file may reference a slug defined only in the project root."""
    home = tmp_path / "home"
    (home / ".cluxmate" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / ".cluxmate" / "agents" / "boss.md").write_text(
        "---\ndescription: b\ntools: [read_file, task]\nsubagents: [helper]\n---\n",
        encoding="utf-8",
    )
    _write_agent(tmp_path, "helper", "---\ndescription: h\n---\n")
    types = _reg(tmp_path, home).types()
    assert "boss" in types and "helper" in types
    assert types["boss"].subagents == ("helper",)
    assert types["boss"].source == "global"


def test_non_utf8_file_is_dropped_in_isolation(tmp_path, monkeypatch):
    """An arbitrary-bytes .md lands in errors() and must not take down the
    registry — every other valid type still loads."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    d = tmp_path / ".cluxmate" / "agents"
    d.mkdir(parents=True)
    (d / "gbk.md").write_bytes(b"---\ndescription: \xff\xfe\n---\n")
    _write_agent(tmp_path, "ok", "---\ndescription: fine\n---\n")
    reg = _reg(tmp_path)
    types = reg.types()  # must not raise
    assert "ok" in types and "gbk" not in types
    assert any("unreadable" in e["error"] for e in reg.errors())


def test_new_file_seen_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    reg = _reg(tmp_path)
    assert "later" not in reg.types()
    _write_agent(tmp_path, "later", "---\ndescription: x\n---\n")
    assert "later" in reg.types()


def test_snapshot_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    snap = _reg(tmp_path).snapshot()
    assert [a["slug"] for a in snap["agents"]][:3] == [
        "general-purpose", "explore", "reviewer"
    ]
    first = snap["agents"][0]
    assert set(first) == {
        "slug", "name", "description", "tools", "readonly", "model",
        "max_turns", "builtin", "source", "path",
    }
    assert snap["errors"] == []


def test_builtin_definitions_stay_intact():
    gp = BUILTIN_AGENT_TYPES["general-purpose"]
    ex = BUILTIN_AGENT_TYPES["explore"]
    assert "task" in gp.tools and "bash" in gp.tools and "write_file" in gp.tools
    assert gp.readonly is False and gp.max_turns == 150
    assert "task" in ex.tools and ex.readonly is True
    assert ex.subagents_mode == "list" and ex.subagents == ("explore",)
    assert ex.max_turns == MAX_AGENT_TURNS


def test_builtin_reviewer_can_verify_but_never_edit():
    """The reviewer holds exactly the tools its contract needs: read a change,
    run the command that proves a claim — and nothing that could make or fix
    the change it is judging."""
    rv = BUILTIN_AGENT_TYPES["reviewer"]
    assert set(rv.tools) == {"read_file", "grep", "list_dir", "lsp", "bash"}
    assert rv.can_edit is False
    assert not (set(rv.tools) & EDIT_TOOL_NAMES)
    # No recursion: a reviewer reviews.
    assert "task" not in rv.tools
    assert rv.subagents_mode == "none"
    # Holding bash makes it a scheduling writer; a reviewer must not race the
    # edits it judges, but it must not claim a write scope either.
    assert rv.readonly is False
    assert rv.builtin is True
    assert rv.instructions.strip()
    # A review that dies at the turn cap delivers nothing, and the measured cost
    # of a real review is ~50 turns, so the reviewer gets the full agent budget
    # rather than the 50 a user-defined type defaults to.
    assert rv.max_turns == MAX_AGENT_TURNS > DEFAULT_SUBAGENT_MAX_TURNS


def test_can_edit_separates_running_from_editing():
    """`readonly` (the scheduling question) and `can_edit` (what the child prompt
    may claim) are different questions — bash collapses them otherwise."""
    gp = BUILTIN_AGENT_TYPES["general-purpose"]
    ex = BUILTIN_AGENT_TYPES["explore"]
    rv = BUILTIN_AGENT_TYPES["reviewer"]
    assert (gp.can_edit, gp.readonly) == (True, False)
    assert (ex.can_edit, ex.readonly) == (False, True)
    assert (rv.can_edit, rv.readonly) == (False, False)


def test_reviewer_instructions_carry_the_gate_contract():
    text = BUILTIN_AGENT_TYPES["reviewer"].instructions
    # The requirement taxonomy: in/out of scope per claim.
    assert "out of scope" in text and "in-scope" in text
    # Evidence or it is a fail; prose is not evidence.
    assert "file:line" in text and "is NOT evidence" in text
    # Silence is not a pass, and neither is an unsupported one.
    assert "unverifiable" in text
    # The verdict vocabulary must be what tools/task.py parses back.
    assert "**Status**: success" in text and "**Status**: partial" in text
