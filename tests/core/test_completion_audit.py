"""Tests for the turn-end completion audit (claim-vs-evidence)."""

import json

import pytest

from cluxmate.core import completion_audit as ca
from cluxmate.core.agent import AgentLoop
from cluxmate.core.completion_audit import (
    WRITE_TOOLS,
    audit_completion,
    normalize_path,
    resolve_file_touched,
    tool_write_paths,
)
from cluxmate.core.providers.base import LLMResponse, ToolCall
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.tools.base import BaseTool, ToolBridge


# ---------------------------------------------------------------- pure audit


def test_no_claims_no_fire():
    assert audit_completion("All right, here is my analysis of the code.") is None
    assert audit_completion("") is None
    assert audit_completion("好的。") is None


def test_bare_done_without_file_or_test_token_does_not_fire():
    # A bare "done" carries no verifiable object — the audit stays quiet.
    assert audit_completion("完成。") is None
    assert audit_completion("Done.") is None


def test_file_claim_with_zero_tools_fires():
    r = audit_completion(
        "I fixed the parser bug in src/utils.py. Everything is done.",
        tool_calls_made=0,
    )
    assert r is not None
    assert "utils.py" in r


def test_file_claim_with_zero_tools_chinese_fires():
    r = audit_completion("已修改 cli.py 中的解析逻辑，问题已修复。")
    assert r is not None
    assert "cli.py" in r


def test_negated_file_claim_does_not_fire():
    r = audit_completion(
        "I did not modify utils.py; the change still needs to be made.",
        tool_calls_made=0,
    )
    assert r is None
    r = audit_completion("没有修改 cli.py，该任务尚未完成。")
    assert r is None


def test_backed_file_claim_does_not_fire():
    r = audit_completion(
        "I fixed src/utils.py.",
        write_paths=["src/utils.py"],
        tool_calls_made=1,
    )
    assert r is None
    # basename match against a differently-rooted path
    r = audit_completion(
        "I fixed utils.py.",
        write_paths=["/work/repo/src/utils.py"],
        tool_calls_made=1,
    )
    assert r is None


def test_mismatched_file_claim_fires():
    r = audit_completion(
        "I fixed utils.py.",
        write_paths=["other.py"],
        tool_calls_made=1,
    )
    assert r is not None
    assert "utils.py" in r


def test_file_claim_with_reads_only_fires():
    # Only read tools ran this turn — nothing could have changed.
    r = audit_completion(
        "I fixed utils.py.",
        write_paths=[],
        tool_calls_made=3,
    )
    assert r is not None


def test_file_claim_with_bash_gets_benefit_of_doubt():
    # v1 does no command-semantics matching; a bash call suppresses the
    # path-level check (documented conservative behavior).
    r = audit_completion(
        "I fixed utils.py.",
        write_paths=[],
        any_bash=True,
        tool_calls_made=1,
    )
    assert r is None


def test_test_claim_with_no_bash_fires():
    r = audit_completion("All tests pass, the build is green.")
    assert r is not None
    assert "bash" in r
    r = audit_completion("测试通过，构建成功。")
    assert r is not None


def test_negated_test_claim_does_not_fire():
    assert audit_completion("The tests did not pass yet.") is None
    assert audit_completion("无法测试：环境不可用。") is None


def test_test_claim_with_bash_gets_benefit_of_doubt():
    r = audit_completion("All tests pass.", any_bash=True, tool_calls_made=1)
    assert r is None


# ------------------------------------------------ filesystem fallback (B-lite)


def test_bash_file_claim_verified_by_filesystem():
    r = audit_completion(
        "I fixed utils.py.",
        any_bash=True,
        tool_calls_made=1,
        resolve_touched=lambda n: True,
    )
    assert r is None


def test_bash_file_claim_refuted_by_filesystem():
    r = audit_completion(
        "I fixed utils.py.",
        any_bash=True,
        tool_calls_made=1,
        resolve_touched=lambda n: False,
    )
    assert r is not None
    assert "filesystem" in r


def test_bash_file_claim_unknown_filesystem_skips():
    # scan budget exhausted => unknown => conservative skip, never bounce
    r = audit_completion(
        "I fixed utils.py.",
        any_bash=True,
        tool_calls_made=1,
        resolve_touched=lambda n: None,
    )
    assert r is None


def test_bash_file_claim_without_resolver_skips():
    r = audit_completion("I fixed utils.py.", any_bash=True, tool_calls_made=1)
    assert r is None


def test_deletion_claim_skipped_by_filesystem_check():
    # a deleted file looks like one that never existed — mtime can't decide
    r = audit_completion(
        "I deleted utils.py.",
        any_bash=True,
        tool_calls_made=1,
        resolve_touched=lambda n: False,
    )
    assert r is None


class _Stat:
    def __init__(self, mtime):
        self.st_mtime = mtime
        # st_size/st_mode keep pathlib/linecache consumers (which pytest itself
        # uses when formatting a failure report) from crashing on our fake.
        self.st_size = 0
        self.st_mode = 0o100644


def test_resolve_file_touched_absolute_path(monkeypatch):
    monkeypatch.setattr(ca.os, "stat", lambda p, *a, **k: _Stat(200.0))
    assert resolve_file_touched("/work/src/utils.py", "/cwd", 100.0) is True
    assert resolve_file_touched("/work/src/utils.py", "/cwd", 300.0) is False
    monkeypatch.setattr(
        ca.os, "stat", lambda p, *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert resolve_file_touched("/work/src/missing.py", "/cwd", 100.0) is False


def test_resolve_file_touched_walk_finds_basename(monkeypatch):
    tree = [
        ("/cwd", ["node_modules", "src"], ["readme.md"]),
        ("/cwd/src", [], ["Utils.PY"]),
    ]
    monkeypatch.setattr(ca.os, "walk", lambda cwd: iter(tree))

    monkeypatch.setattr(
        ca.os,
        "stat",
        lambda p, *a, **k: _Stat(200.0 if "utils.py" in p.lower() else 1.0),
    )
    assert resolve_file_touched("utils.py", "/cwd", 100.0) is True
    # exists but untouched this turn
    monkeypatch.setattr(ca.os, "stat", lambda p, *a, **k: _Stat(1.0))
    assert resolve_file_touched("utils.py", "/cwd", 100.0) is False


def test_resolve_file_touched_walk_budget_exhausted_is_unknown(monkeypatch):
    monkeypatch.setattr(ca, "_SCAN_FILE_BUDGET", 2)
    tree = [("/cwd", [], ["a.py", "b.py", "c.py", "utils.py"])]
    monkeypatch.setattr(ca.os, "walk", lambda cwd: iter(tree))
    assert resolve_file_touched("utils.py", "/cwd", 100.0) is None


def test_resolve_file_touched_skips_heavy_dirs(monkeypatch):
    seen: list[list[str]] = []

    def walk(cwd):
        dirs = [".git", "node_modules", "src"]
        seen.append(dirs)
        yield (cwd, dirs, [])

    def stat(path, *args, **kwargs):
        # Report the file as missing: resolve_file_touched checks
        # os.path.exists(cwd/name) first, and a fake stat that returns a
        # valid-looking result for every path would fire that shortcut and
        # skip the os.walk branch this test is about.
        raise FileNotFoundError(path)

    monkeypatch.setattr(ca.os, "walk", walk)
    monkeypatch.setattr(ca.os, "stat", stat)
    resolve_file_touched("utils.py", "/cwd", 100.0)
    assert seen[0] == ["src"]  # heavy dirs pruned in place


def test_tool_write_paths_extraction():
    assert tool_write_paths("write_file", {"path": "a.py"}) == ["a.py"]
    assert tool_write_paths("search_replace", {"path": "b.py"}) == ["b.py"]
    assert tool_write_paths("delete_file", {"path": "c.py"}) == ["c.py"]
    assert tool_write_paths(
        "multi_edit", {"edits": [{"path": "d.py"}, {"path": "e.py"}]}
    ) == ["d.py", "e.py"]
    assert tool_write_paths(
        "multi_write", {"files": [{"path": "f.py"}, {"path": "g.py"}]}
    ) == ["f.py", "g.py"]
    assert tool_write_paths("read_file", {"path": "h.py"}) == []
    assert tool_write_paths("bash", {"command": "ls"}) == []


def test_normalize_path():
    assert normalize_path("C:\\work\\src\\Utils.PY") == "utils.py"
    assert normalize_path("/work/repo/src/utils.py") == "utils.py"


# ---------------------------------------------------------------- loop wiring


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls.append((messages, tools))
        resp = self.responses.pop(0) if self.responses else LLMResponse(
            text="done", stop_reason="end_turn"
        )
        if on_delta and resp.text:
            await on_delta(resp.text)
        return resp

    def assistant_message_to_api(self, msg):
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result):
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": result.content,
        }

    def max_tokens(self):
        return 1000


class FakeWriteTool(BaseTool):
    @property
    def name(self):
        return "write_file"

    @property
    def description(self):
        return "Write."

    @property
    def input_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        }

    async def execute(self, path: str = "", content: str = ""):
        return f"WROTE {path}"


def make_log() -> SessionLog:
    return SessionLog.create(SessionHeader(id="s1", createdAt=0, apiType="openai"))


@pytest.mark.asyncio
async def test_fake_completion_bounced_and_corrected():
    log = make_log()
    provider = RecordingProvider([
        LLMResponse(
            text="I fixed the bug in utils.py and all tests pass.",
            stop_reason="end_turn",
        ),
        LLMResponse(
            text="Corrected: the bug in utils.py is still present; I did not "
                 "change anything.",
            stop_reason="end_turn",
        ),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("Fix the bug in utils.py")
    assert result.text.startswith("Corrected:")

    audit_msgs = [
        e.data for e in log.events
        if e.type == "user/message" and e.data["source"] == "completion-audit"
    ]
    assert len(audit_msgs) == 1
    assert "utils.py" in audit_msgs[0]["message"]["content"]
    # the rejected reply never entered the surface
    assert "I fixed the bug" not in log.derive_messages()[-1]["content"]
    # audit trail on turn/end
    reason = log.events[-1].data["reason"]
    assert reason["kind"] == "completed"
    assert reason["completion_audit"] == {"reminders": 1}
    # the audit message is model-visible (logged ⟺ visible)
    assert any(
        m["role"] == "user" and "Completion audit:" in m["content"]
        for m in log.derive_messages()
    )


@pytest.mark.asyncio
async def test_audit_bounce_capped_at_one():
    log = make_log()
    provider = RecordingProvider([
        LLMResponse(text="I fixed utils.py.", stop_reason="end_turn"),
        LLMResponse(text="I fixed utils.py.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("Fix the bug")
    # second identical claim is committed as-is (advisory, bounded)
    assert result.text == "I fixed utils.py."
    assert log.events[-1].data["reason"]["completion_audit"] == {"reminders": 1}
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_backed_claim_not_bounced():
    log = make_log()
    bridge = ToolBridge()
    bridge.register(FakeWriteTool())
    provider = RecordingProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="write_file",
                                 input={"path": "utils.py", "content": "x"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="I fixed utils.py.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("Fix the bug")
    assert result.text == "I fixed utils.py."
    sources = [
        e.data.get("source") for e in log.events if e.type == "user/message"
    ]
    assert "completion-audit" not in sources
    assert "completion_audit" not in log.events[-1].data["reason"]


@pytest.mark.asyncio
async def test_honest_reply_not_bounced():
    log = make_log()
    provider = RecordingProvider([
        LLMResponse(
            text="I could not finish: the tests do not pass yet and "
                 "utils.py still has the bug.",
            stop_reason="end_turn",
        ),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=ToolBridge(),
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("Fix the bug")
    assert result.text.startswith("I could not finish:")
    assert len(provider.calls) == 1
    sources = [
        e.data.get("source") for e in log.events if e.type == "user/message"
    ]
    assert "completion-audit" not in sources


class FakeBashTool(BaseTool):
    @property
    def name(self):
        return "bash"

    @property
    def description(self):
        return "Bash."

    @property
    def input_schema(self):
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        }

    async def execute(self, command: str = ""):
        return f"OK: {command}"


@pytest.mark.asyncio
async def test_bash_edit_claim_bounced_via_filesystem(monkeypatch):
    # bash ran successfully (any_bash=True), so the tool record can't verify
    # the claim — the filesystem resolver refutes it and the reply bounces.
    import cluxmate.core.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "resolve_file_touched", lambda name, cwd=None, turn_start_ts=None: False
    )
    log = make_log()
    bridge = ToolBridge()
    bridge.register(FakeBashTool())
    provider = RecordingProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="bash",
                                 input={"command": "sed -i x utils.py"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="I fixed utils.py via bash.", stop_reason="end_turn"),
        LLMResponse(
            text="Corrected: utils.py was not actually modified.",
            stop_reason="end_turn",
        ),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="s",
        session_log=log,
        cwd="C:/fake-repo",
    )
    result = await agent.run("Fix the bug")
    assert result.text.startswith("Corrected:")
    audit_msgs = [
        e.data for e in log.events
        if e.type == "user/message" and e.data["source"] == "completion-audit"
    ]
    assert len(audit_msgs) == 1
    assert "filesystem" in audit_msgs[0]["message"]["content"]
    assert log.events[-1].data["reason"]["completion_audit"] == {"reminders": 1}


@pytest.mark.asyncio
async def test_bash_edit_claim_backed_by_filesystem_not_bounced(monkeypatch):
    import cluxmate.core.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "resolve_file_touched", lambda name, cwd=None, turn_start_ts=None: True
    )
    log = make_log()
    bridge = ToolBridge()
    bridge.register(FakeBashTool())
    provider = RecordingProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="bash",
                                 input={"command": "sed -i x utils.py"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="I fixed utils.py via bash.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="s",
        session_log=log,
        cwd="C:/fake-repo",
    )
    result = await agent.run("Fix the bug")
    assert result.text == "I fixed utils.py via bash."
    assert len(provider.calls) == 2
    sources = [
        e.data.get("source") for e in log.events if e.type == "user/message"
    ]
    assert "completion-audit" not in sources


@pytest.mark.asyncio
async def test_bash_edit_claim_without_cwd_keeps_v1_skip():
    # No cwd => no resolver => bash turns keep the conservative v1 skip.
    log = make_log()
    bridge = ToolBridge()
    bridge.register(FakeBashTool())
    provider = RecordingProvider([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="bash",
                                 input={"command": "sed -i x utils.py"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="I fixed utils.py via bash.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="s",
        session_log=log,
    )
    result = await agent.run("Fix the bug")
    assert result.text == "I fixed utils.py via bash."
    assert len(provider.calls) == 2
    sources = [
        e.data.get("source") for e in log.events if e.type == "user/message"
    ]
    assert "completion-audit" not in sources
