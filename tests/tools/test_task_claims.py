"""`task` write_paths → scheduler claim wiring."""

from pathlib import Path

from cluxmate.core.subagents import BUILTIN_AGENT_TYPES
from cluxmate.tools.task import TaskTool


class _Builder:
    def __init__(self, cwd: str, profile=BUILTIN_AGENT_TYPES["general-purpose"], depth: int = 0):
        self.cwd = cwd
        self._profile = profile
        self._depth = depth

    def allowed_subagent_slugs(self):
        return [self._profile.slug]

    def agent_type(self, slug):
        return self._profile

    @property
    def depth(self):
        return self._depth


def test_omitted_write_paths_claim_the_workspace(tmp_path):
    tool = TaskTool(_Builder(str(tmp_path)))
    claim, dropped = tool._claim_for(BUILTIN_AGENT_TYPES["general-purpose"], None)
    assert claim == frozenset({tmp_path.resolve()})
    assert dropped == []


def test_declared_write_paths_are_resolved_and_escapes_dropped(tmp_path):
    tool = TaskTool(_Builder(str(tmp_path)))
    claim, dropped = tool._claim_for(
        BUILTIN_AGENT_TYPES["general-purpose"], ["src", "../etc"]
    )
    assert claim == frozenset({(tmp_path / "src").resolve()})
    assert dropped == ["../etc"]


def test_read_only_type_never_claims(tmp_path):
    tool = TaskTool(_Builder(str(tmp_path)))
    claim, dropped = tool._claim_for(BUILTIN_AGENT_TYPES["explore"], None)
    assert claim == frozenset() and dropped == []
    claim, _ = tool._claim_for(BUILTIN_AGENT_TYPES["explore"], ["src"])
    assert claim == frozenset()


def test_all_dropped_write_paths_fall_back_to_workspace_claim(tmp_path):
    """Every declared entry outside the workspace: the claim falls back to the
    whole workspace instead of empty (an empty claim would read as read-only
    and skip serialization entirely — a useless declaration must not buy MORE
    parallelism than no declaration at all)."""
    tool = TaskTool(_Builder(str(tmp_path)))
    claim, dropped = tool._claim_for(
        BUILTIN_AGENT_TYPES["general-purpose"], ["../downloads", "../../tmp"]
    )
    assert claim == frozenset({tmp_path.resolve()})
    assert dropped == ["../downloads", "../../tmp"]


def test_non_list_write_paths_treated_as_not_declared(tmp_path):
    """A model passing a string (schema violation) must not become char paths."""
    tool = TaskTool(_Builder(str(tmp_path)))
    claim, dropped = tool._claim_for(BUILTIN_AGENT_TYPES["general-purpose"], "src")
    assert claim == frozenset({tmp_path.resolve()})
    assert dropped == []


def test_nested_flag_follows_builder_depth(tmp_path):
    assert TaskTool(_Builder(str(tmp_path), depth=0))._nested() is False
    assert TaskTool(_Builder(str(tmp_path), depth=1))._nested() is True


def test_schema_advertises_write_paths(tmp_path):
    tool = TaskTool(_Builder(str(tmp_path)))
    props = tool.input_schema["properties"]
    assert props["write_paths"]["type"] == "array"
    assert props["subagent_type"]["enum"] == ["general-purpose"]
