"""project_findings — what makes a directory worth asking about."""

import os
from pathlib import Path

from cluxmate.core.trust import project_findings


def _kinds(cwd: Path) -> list[str]:
    return [f.kind for f in project_findings(str(cwd))]


def _state(tmp_path: Path) -> Path:
    d = tmp_path / ".cluxmate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_empty_directory_has_nothing_to_gate(tmp_path):
    assert project_findings(str(tmp_path)) == []


def test_runtime_artifacts_do_not_count(tmp_path):
    state = _state(tmp_path)
    (state / "sandbox-il-applied").write_text("1", encoding="utf-8")
    (state / "tmp-low").mkdir()
    (state / "tmp-spill").mkdir()
    assert project_findings(str(tmp_path)) == []


def test_each_config_file_is_reported(tmp_path):
    state = _state(tmp_path)
    for name in (
        "settings.json",
        "mcp.json",
        "lsp.json",
        "skills.json",
        "permissions.json",
    ):
        (state / name).write_text("{}", encoding="utf-8")
    assert _kinds(tmp_path) == ["hooks", "mcp", "lsp", "skills", "permissions"]


def test_directories_are_probed_by_glob(tmp_path):
    state = _state(tmp_path)
    (state / "agents").mkdir()
    (state / "agents" / "reviewer.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    (state / "skills" / "demo").mkdir(parents=True)
    (state / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (state / "memory" / "facts").mkdir(parents=True)
    (state / "memory" / "facts" / "abc.md").write_text("fact", encoding="utf-8")
    assert _kinds(tmp_path) == ["skills", "agents", "facts"]


def test_a_skill_dir_reports_once(tmp_path):
    state = _state(tmp_path)
    (state / "skills" / "demo").mkdir(parents=True)
    (state / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (state / "skills.json").write_text("{}", encoding="utf-8")
    assert _kinds(tmp_path) == ["skills"]


def test_an_empty_agents_dir_is_not_a_finding(tmp_path):
    state = _state(tmp_path)
    (state / "agents").mkdir()
    (state / "memory" / "facts").mkdir(parents=True)
    assert project_findings(str(tmp_path)) == []


def test_paths_are_relative_to_the_working_directory(tmp_path):
    state = _state(tmp_path)
    (state / "mcp.json").write_text("{}", encoding="utf-8")
    (finding,) = project_findings(str(tmp_path))
    rel = f".cluxmate{os.sep}mcp.json"
    assert finding.label == "MCP servers (mcp.json)"
    assert finding.path == rel
    assert finding.as_dict() == {"kind": "mcp", "label": "MCP servers (mcp.json)", "path": rel}


def test_decision_reports_gated_only_with_findings(tmp_path):
    from cluxmate.core.trust import TrustStore, resolve_trust

    state = _state(tmp_path)
    (state / "settings.json").write_text("{}", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    store = TrustStore(tmp_path / "trust.json")
    assert resolve_trust(str(tmp_path), store).gated is True
    assert resolve_trust(str(tmp_path), store).pending is True
    assert resolve_trust(str(empty), store).gated is False
    store.set(str(tmp_path), "trusted")
    assert resolve_trust(str(tmp_path), store).gated is False
