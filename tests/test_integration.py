"""Smoke tests for the full agent pipeline."""

import asyncio

import pytest
from cluxmate.core.agent import (
    NETWORK_FALLBACK_TEXT,
    AgentCallbacks,
    AgentLoop,
    AgentResult,
)
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.providers.base import LLMProvider, LLMResponse, ToolCall
from cluxmate.tools.base import BaseTool, ToolBridge
from typing import Any
from pathlib import Path


class FakeProvider:
    """Fake LLM provider that returns a text response then stops."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = responses
        self.calls: list[tuple] = []

    async def chat(
        self, messages: list[dict], tools: list[dict],
        *, on_delta=None, on_thinking=None,
    ) -> LLMResponse:
        self.calls.append((messages, tools))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="done", stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "user", "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class EchoTool(BaseTool):
    """A simple tool that echoes its input."""

    @property
    def name(self) -> str: return "echo"

    @property
    def description(self) -> str: return "Echo back the input."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    async def execute(self, message: str = "") -> str:
        return f"ECHO: {message}"


@pytest.mark.asyncio
async def test_simple_text_response():
    """Agent loop returns text when model responds without tools."""
    provider = FakeProvider([
        LLMResponse(text="Hello, world!", stop_reason="end_turn"),
    ])
    bridge = ToolBridge()
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="You are a test agent.",
    )

    result = await agent.run("Hi")
    assert result.text == "Hello, world!"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_max_tokens_stop_with_empty_text_surfaces_marker():
    """A thinking model can burn its entire output budget on reasoning — the
    turn then stops as max_tokens with NO content (the real-world Qwen
    failure). The loop must return an explanatory marker instead of leaving
    the UI with a silent empty '(no output)' bubble."""
    provider = FakeProvider([
        LLMResponse(text="", stop_reason="max_tokens"),
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(), system_prompt="Test",
    )

    result = await agent.run("Hi")
    assert "truncated" in result.text.lower()
    assert "1000" in result.text  # budget from FakeProvider.max_tokens()
    # The marker is also the assistant turn in history, so a follow-up user
    # message lets the model see what happened.
    assert result.history[-1] == {"role": "assistant", "content": result.text}


@pytest.mark.asyncio
async def test_max_tokens_stop_with_partial_text_flags_truncation():
    """A reply cut off mid-generation keeps its partial text but carries a
    truncation notice so nobody mistakes it for a complete answer."""
    provider = FakeProvider([
        LLMResponse(text="partial answer", stop_reason="max_tokens"),
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(), system_prompt="Test",
    )

    result = await agent.run("Hi")
    assert result.text.startswith("partial answer")
    assert "truncated" in result.text.lower()


@pytest.mark.asyncio
async def test_empty_end_turn_surfaces_marker():
    """end_turn with no text and no tool calls (thinking-model behavioral
    quirk) gets an honest marker instead of an empty bubble — but only after the
    bounded silent retry is also exhausted. Two empties in a row (original +
    MAX_EMPTY_END_TURN_RETRIES=1) surface the marker."""
    provider = FakeProvider([
        LLMResponse(text="", stop_reason="end_turn"),
        LLMResponse(text="", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(), system_prompt="Test",
    )

    result = await agent.run("Hi")
    assert "empty response" in result.text.lower()


@pytest.mark.asyncio
async def test_empty_end_turn_retries_then_recovers():
    """A transient empty end_turn is silently retried; the re-issued request
    returning real text is what the user sees — no marker, no manual resend."""
    provider = FakeProvider([
        LLMResponse(text="", stop_reason="end_turn"),          # transient miss
        LLMResponse(text="Real answer.", stop_reason="end_turn"),  # retry succeeds
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(), system_prompt="Test",
    )

    result = await agent.run("Hi")
    assert result.text == "Real answer."
    assert "empty response" not in result.text.lower()
    # Both responses were consumed — the retry actually happened.
    assert provider.responses == []


@pytest.mark.asyncio
async def test_single_tool_call_loop():
    """Agent loop calls a tool, feeds result back, then returns text."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(id="tc1", name="echo", input={"message": "ping"})
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Tool complete. Done.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="Test",
    )

    result = await agent.run("Run echo")
    assert result.text == "Tool complete. Done."
    assert result.tool_calls_made == 1
    all_messages = provider.calls[1][0]
    tool_results = [m for m in all_messages if m.get("role") == "user" and "ECHO" in m.get("content", "")]
    assert len(tool_results) == 1


@pytest.mark.asyncio
async def test_malformed_tool_arguments_do_not_crash():
    """A tool call the provider couldn't parse yields an error result (fed back
    to the model to retry) instead of crashing the turn."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    provider = FakeProvider([
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="bad1", name="echo", input={},
                    parse_error="Expecting ',' delimiter: line 1 column 88 (char 87)",
                )
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Recovered.", stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge, system_prompt="Test",
    )

    result = await agent.run("Do the thing")
    assert result.text == "Recovered."
    # The malformed call's error is fed back so the model can retry.
    followup_messages = provider.calls[1][0]
    err = [
        m for m in followup_messages
        if m.get("role") == "user" and "could not parse tool arguments" in m.get("content", "")
    ]
    assert len(err) == 1


def test_parse_tool_arguments_defensive():
    """The shared defensive parser degrades on malformed JSON without raising."""
    from cluxmate.core.providers.base import parse_tool_arguments

    assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, None)
    assert parse_tool_arguments("") == ({}, None)
    # Unescaped inner quote — the real DeepSeek failure mode.
    parsed, err = parse_tool_arguments('{"new_string": "print("x")"}')
    assert parsed == {} and err is not None
    # Valid JSON that isn't an object.
    parsed, err = parse_tool_arguments("[1, 2]")
    assert parsed == {} and "object" in err


@pytest.mark.asyncio
async def test_max_turns_safety():
    """Agent loop stops after MAX_TURNS iterations."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    class InfiniteToolProvider(FakeProvider):
        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            return LLMResponse(
                tool_calls=[ToolCall(id="x", name="echo", input={"message": "x"})],
                stop_reason="tool_use",
            )

    provider = InfiniteToolProvider([])
    agent = AgentLoop(
        model="test",
        provider=provider,
        tools=bridge,
        system_prompt="Test",
    )

    result = await agent.run("Go")
    assert "exceeded maximum turns" in result.text.lower()


@pytest.mark.asyncio
async def test_repeat_tool_reminder_injects_advisory_nudge():
    """Three consecutive identical tool calls inject a gentle loop-guard nudge.

    Modeled on DeepSeek Harness's repeat-tool-reminder: the first threshold
    (3) is advisory text telling the model to stop repeating itself. The loop
    never blocks — MAX_TURNS remains the hard backstop."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    class RepeatingProvider(FakeProvider):
        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) <= 3:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"tc{len(self.calls)}",
                            name="echo",
                            input={"message": "x"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(text="done", stop_reason="end_turn")

    provider = RepeatingProvider([])
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge, system_prompt="Test",
    )

    result = await agent.run("Go")
    assert result.text == "done"
    assert result.tool_calls_made == 3

    # The reminder is injected after the third identical call's tool result, so
    # it is visible in the request that follows (the end_turn call).
    final_messages = provider.calls[3][0]
    nudges = [
        m for m in final_messages
        if m.get("role") == "user"
        and "repeating the exact same tool call" in m.get("content", "")
    ]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_repeat_tool_reminder_escalates_to_detailed_form():
    """The later threshold injects the detailed reminder naming the tool, the
    consecutive count, and the canonical arguments."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    class RepeatingProvider(FakeProvider):
        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) <= 6:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"tc{len(self.calls)}",
                            name="echo",
                            input={"message": "x"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(text="done", stop_reason="end_turn")

    provider = RepeatingProvider([])
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge, system_prompt="Test",
    )

    result = await agent.run("Go")
    assert result.tool_calls_made == 6

    final_messages = provider.calls[6][0]
    detailed = [
        m for m in final_messages
        if m.get("role") == "user"
        and "Repeated tool call detected:" in m.get("content", "")
    ]
    assert len(detailed) == 1
    assert "tool: echo" in detailed[0]["content"]
    assert "consecutive_calls: 6" in detailed[0]["content"]
    assert "arguments: " in detailed[0]["content"]


@pytest.mark.asyncio
async def test_repeat_tool_reminder_resets_when_arguments_change():
    """A differing call resets the consecutive-run chain, so x,x,y,y never
    trips the threshold even though four tool calls ran."""
    bridge = ToolBridge()
    bridge.register(EchoTool())

    class Provider(FakeProvider):
        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            n = len(self.calls)
            if n <= 4:
                message = "x" if n <= 2 else "y"
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id=f"tc{n}", name="echo", input={"message": message})
                    ],
                    stop_reason="tool_use",
                )
            return LLMResponse(text="done", stop_reason="end_turn")

    provider = Provider([])
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge, system_prompt="Test",
    )

    result = await agent.run("Go")
    assert result.tool_calls_made == 4

    final_messages = provider.calls[4][0]
    assert not any(
        "repeating the exact same tool call" in m.get("content", "")
        or "Repeated tool call detected:" in m.get("content", "")
        for m in final_messages
        if m.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_main_agent_delegates_to_child_subagent():
    """Main agent calls TaskTool, child subagent runs and returns result."""
    # --- Child subagent provider: responds with text ---
    class ChildProvider:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            # Verify child has NO task tool
            tool_names = [t["name"] for t in tools]
            assert "task" not in tool_names, "child should not have task tool"
            return LLMResponse(
                text="Found 3 API routes: /login, /logout, /users",
                stop_reason="end_turn",
            )

        def assistant_message_to_api(self, msg) -> dict:
            return {"role": "assistant", "content": msg.text or ""}

        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": result.content}

        def max_tokens(self) -> int:
            return 1000

    child_provider = ChildProvider()

    # --- Main agent provider: first turn calls task, second turn returns final ---
    class MainProvider:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                # First call: the model decides to delegate to subagent
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="task",
                            input={
                                "subagent_type": "explore",
                                "description": "Find all API routes",
                                "prompt": "Search the codebase for all API route definitions.",
                            },
                        )
                    ],
                    stop_reason="tool_use",
                )
            else:
                # Second call: after seeing subagent result, returns final answer
                return LLMResponse(
                    text="The project has 3 API routes.",
                    stop_reason="end_turn",
                )

        def assistant_message_to_api(self, msg) -> dict:
            content = []
            if msg.text:
                content.append({"type": "text", "text": msg.text})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })
            return {"role": "assistant", "content": content}

        def tool_result_to_api(self, result) -> dict:
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": result.content,
                }],
            }

        def max_tokens(self) -> int:
            return 1000

    main_provider = MainProvider()

    # --- Build main agent with a special builder that injects the child provider ---
    import os
    cwd = os.getcwd()

    builder = AgentBuilder(cwd, main_provider)
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])

    # Monkey-patch build_child to use child_provider
    original_build_child = builder.build_child

    def patched_build_child(subagent_type, task_description, agent_id=""):
        child = original_build_child(subagent_type, task_description, agent_id)
        # Replace child's provider with our controlled one
        child.provider = child_provider
        return child

    builder.build_child = patched_build_child

    agent = builder.build()

    # Verify main agent has task tool
    tool_names = [d["name"] for d in agent.tools.definitions()]
    assert "task" in tool_names

    # --- Run ---
    result = await agent.run("What API routes exist in this project?")
    assert result.text == "The project has 3 API routes."
    assert result.tool_calls_made == 1  # one task tool call
    assert len(main_provider.calls) == 2  # tool_use + end_turn
    assert len(child_provider.calls) == 1  # child ran once
    # Verify child received the right prompt
    child_messages = child_provider.calls[0][0]
    child_user_msg = next(
        m["content"] for m in child_messages if m["role"] == "user"
    )
    assert "API route definitions" in child_user_msg


@pytest.mark.asyncio
async def test_child_subagent_reports_failure():
    """When child subagent fails, main agent gets error message back."""

    class ChildProvider:
        async def chat(self, messages, tools):
            return LLMResponse(
                text="I cannot complete this task because the file doesn't exist.",
                stop_reason="end_turn",
            )

        def assistant_message_to_api(self, msg) -> dict:
            return {"role": "assistant", "content": msg.text or ""}

        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": result.content}

        def max_tokens(self) -> int:
            return 1000

    class MainProvider:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1", name="task",
                            input={
                                "subagent_type": "explore",
                                "description": "Read nonexistent file",
                                "prompt": "Read /nonexistent/file.txt",
                            },
                        )
                    ],
                    stop_reason="tool_use",
                )
            else:
                # After seeing child's failure report
                second = messages[-1]
                content = second.get("content", "")
                if isinstance(content, list):
                    content = content[0].get("content", "") if content else ""
                assert "cannot complete" in content.lower() or "failed" in content.lower()
                return LLMResponse(
                    text="The subagent couldn't read the file. Let me try another approach.",
                    stop_reason="end_turn",
                )

        def assistant_message_to_api(self, msg) -> dict:
            content = []
            if msg.text:
                content.append({"type": "text", "text": msg.text})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content.append({
                        "type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input,
                    })
            return {"role": "assistant", "content": content}

        def tool_result_to_api(self, result) -> dict:
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": result.content,
                }],
            }

        def max_tokens(self) -> int:
            return 1000

    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, MainProvider())
    builder.with_default_tools()
    builder.with_subagent_types(["explore"])

    original = builder.build_child
    builder.build_child = lambda t, d, aid="": _inject_child(original(t, d, aid), ChildProvider())

    agent = builder.build()
    result = await agent.run("Read the config file")
    assert result.text == "The subagent couldn't read the file. Let me try another approach."


def _inject_child(child, provider):
    child.provider = provider
    return child


def test_plan_mode_hard_isolates_to_readonly_tools():
    """Plan mode registers only read-only tools — no write/exec/delegation tool
    is present, so a write literally cannot be issued (hard isolation)."""
    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.with_mode("plan")

    names = {t.name for t in builder._get_tools()}
    # Read-only tools present.
    assert {"read_file", "grep", "list_dir"} <= names
    # No write/exec/delegation tools — these could mutate the workspace or spawn
    # a writing subagent that bypasses the isolation.
    for forbidden in ("bash", "search_replace", "write_file", "delete_file",
                      "multi_edit", "multi_write", "task", "update_memory"):
        assert forbidden not in names, f"plan mode must not expose {forbidden}"


def test_plan_mode_includes_ask_user_question():
    """Plan mode is read-only but still allows ask_user_question — clarifying
    questions are how plan mode disambiguates without writing anything."""
    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.with_mode("plan")

    names = {t.name for t in builder._get_tools()}
    assert "ask_user_question" in names


def test_subagent_has_no_ask_user_question():
    """Subagents must not ask the user (they would block the parent's task call
    against its timeout) — ask_user_question is parent-only, like update_memory."""
    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])

    child = builder._child_builder("general-purpose", "c1")
    names = {t.name for t in child._get_tools()}
    assert "ask_user_question" not in names


def test_default_mode_keeps_write_tools():
    """Non-plan modes leave the full toolset present (approval, not tool removal,
    governs writes there)."""
    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.with_mode("yolo")  # any non-plan mode

    names = {t.name for t in builder._get_tools()}
    assert {"bash", "write_file", "delete_file", "task"} <= names


def test_depth_cap_withholds_task_tool():
    """A builder at the recursion cap must not expose the `task` tool."""
    import os
    from cluxmate.core.builder import MAX_SUBAGENT_DEPTH

    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])

    # Below the cap: task tool is present.
    builder._depth = MAX_SUBAGENT_DEPTH - 1
    assert "task" in [t.name for t in builder._get_tools()]

    # At the cap: task tool is withheld to stop runaway nesting.
    builder._depth = MAX_SUBAGENT_DEPTH
    assert "task" not in [t.name for t in builder._get_tools()]


def test_general_purpose_child_can_recurse():
    """A general-purpose child keeps subagent types (can spawn grandchildren);
    an explore child can only spawn explore grandchildren (read-only chain)."""
    import os
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])

    gp = builder._child_builder("general-purpose", "c1")
    assert gp._depth == 1
    assert gp._subagent_types == ["general-purpose", "explore"]
    assert "task" in [t.name for t in gp._get_tools()]

    ex = builder._child_builder("explore", "c2")
    assert ex._depth == 1
    assert ex._subagent_types == ["explore"]
    assert "task" in [t.name for t in ex._get_tools()]


@pytest.mark.asyncio
async def test_explore_child_can_only_spawn_explore():
    """An explore child may recurse, but only within the read-only type: a
    request for a general-purpose grandchild is rejected so the chain can
    never gain write access."""
    import os
    from cluxmate.tools.task import TaskTool
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, FakeProvider([]))
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])

    ex = builder._child_builder("explore", "c1")
    assert "task" in [t.name for t in ex._get_tools()]

    tool = TaskTool(ex)
    # The tool schema advertises only what this agent may spawn, so the model
    # never even tries a type the allowlist would reject.
    schema = tool.input_schema
    assert schema["properties"]["subagent_type"]["enum"] == ["explore"]
    # explore -> explore grandchild is allowed
    ok = await tool.execute(subagent_type="explore", description="d", prompt="p")
    assert "not allowed" not in ok
    # explore -> general-purpose is denied (would break read-only isolation)
    denied = await tool.execute(
        subagent_type="general-purpose", description="d", prompt="p"
    )
    assert "not allowed" in denied and "general-purpose" in denied


class _RecordingTracker:
    """Captures on_agent_start/on_agent_end. Hands out no-op scoped cbs so the
    subagent lifecycle is observed without needing the real callbacks."""

    def __init__(self):
        self.starts: list[tuple] = []
        self.ends: list[tuple] = []

    async def on_agent_start(self, agent_id, parent_id, subagent_type, description, depth, prompt=""):
        self.starts.append((agent_id, parent_id, subagent_type, description, depth))

    async def on_agent_end(self, agent_id, status, result, input_tokens=0, output_tokens=0):
        self.ends.append((agent_id, status, result))

    def scoped(self, agent_id, auto_approve=True):
        return None  # children run without callbacks in this test


class _RouterProvider:
    """One provider instance shared by every agent in the tree. Routes by a
    marker in the latest user message, so it survives recursion (each builder
    reuses this same instance) without a per-agent call counter getting crossed.

    - ROOT_TASK  -> delegate to a general-purpose child (CHILD_TASK)
    - CHILD_TASK -> delegate to an explore grandchild (GC_TASK)
    - GC_TASK    -> plain text (leaf)
    After a tool_result comes back (no marker in the last user text), answer.
    """

    def _last_user_text(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, list):
                    # tool_result message — not a fresh task prompt
                    return ""
                return c
        return ""

    async def chat(self, messages, tools):
        text = self._last_user_text(messages)
        if "ROOT_TASK" in text:
            return LLMResponse(tool_calls=[ToolCall(id="c1", name="task", input={
                "subagent_type": "general-purpose", "description": "child work",
                "prompt": "CHILD_TASK",
            })], stop_reason="tool_use")
        if "CHILD_TASK" in text:
            return LLMResponse(tool_calls=[ToolCall(id="g1", name="task", input={
                "subagent_type": "explore", "description": "grandchild work",
                "prompt": "GC_TASK",
            })], stop_reason="tool_use")
        if "GC_TASK" in text:
            return LLMResponse(text="grandchild done", stop_reason="end_turn")
        # A tool_result came back; the delegating agent now answers.
        return LLMResponse(text="answered", stop_reason="end_turn")

    def assistant_message_to_api(self, msg):
        content = []
        if msg.text:
            content.append({"type": "text", "text": msg.text})
        for tc in (msg.tool_calls or []):
            content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        return {"role": "assistant", "content": content}

    def tool_result_to_api(self, result):
        return {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": result.tool_call_id, "content": result.content,
        }]}

    def max_tokens(self):
        return 1000


@pytest.mark.asyncio
async def test_two_level_delegation_tracks_parent_and_depth():
    """A general-purpose child that itself delegates fires agent_start/end with
    correct parent_id links and increasing depth. Recursion works because the
    child builder inherits subagent types + tracker from its parent."""
    import os

    tracker = _RecordingTracker()
    cwd = os.getcwd()
    builder = AgentBuilder(cwd, _RouterProvider())
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.set_tracker(tracker)

    agent = builder.build()
    result = await agent.run("ROOT_TASK")

    assert result.text == "answered"
    # Two agents started: the general-purpose child (parent root, depth 1) and
    # the explore grandchild (parent = child's id, depth 2).
    assert len(tracker.starts) == 2
    child_start = next(s for s in tracker.starts if s[2] == "general-purpose")
    gc_start = next(s for s in tracker.starts if s[2] == "explore")
    assert child_start[1] == "root" and child_start[4] == 1
    assert gc_start[1] == child_start[0] and gc_start[4] == 2
    # Both ended "done".
    assert len(tracker.ends) == 2
    assert {e[1] for e in tracker.ends} == {"done"}


@pytest.mark.asyncio
async def test_multi_edit_round_trip():
    """Agent calls multi_edit across two files, both are modified."""
    import tempfile
    from cluxmate.tools.multi_edit import MultiEditTool

    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a.txt"
        a.write_text("one\ntwo\n")
        b = Path(tmp) / "b.txt"
        b.write_text("alpha\nbeta\n")

        provider = FakeProvider([
            # Turn 1: multi_edit tool call
            LLMResponse(
                text="I'll edit both files.",
                tool_calls=[
                    ToolCall(id="tc1", name="multi_edit", input={
                        "edits": [
                            {"path": "a.txt", "old_string": "one", "new_string": "1"},
                            {"path": "b.txt", "old_string": "alpha", "new_string": "a"},
                        ],
                    }),
                ],
                stop_reason="tool_use",
            ),
            # Turn 2: model sees tool result and finishes
            LLMResponse(
                text="Both files updated.",
                stop_reason="end_turn",
            ),
        ])

        bridge = ToolBridge()
        bridge.register(MultiEditTool(tmp))

        agent = AgentLoop(
            model="test",
            provider=provider,
            tools=bridge,
            system_prompt="You are a helpful agent.",
        )
        result = await agent.run("Edit both files.", history=[])

        assert a.read_text() == "1\ntwo\n"
        assert b.read_text() == "a\nbeta\n"
        assert result.tool_calls_made == 1


@pytest.mark.asyncio
async def test_ask_user_question_round_trip():
    """The loop pauses on ask_user_question via callbacks.ask_question, injects
    the answer into the tool, and feeds the tool result back to the model."""
    from cluxmate.tools.ask_user_question import AskUserQuestionTool

    bridge = ToolBridge()
    bridge.register(AskUserQuestionTool(builder=None))

    provider = FakeProvider([
        LLMResponse(
            tool_calls=[ToolCall(
                id="q1", name="ask_user_question",
                input={"questions": [
                    {"id": "mode", "question": "Which mode?", "header": "Choose Mode",
                     "options": [{"label": "Plan"}, {"label": "Default"}]},
                ]},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="You chose Default.", stop_reason="end_turn"),
    ])

    class _Asker(AgentCallbacks):
        def __init__(self):
            self.asked = []

        async def ask_question(self, questions, call_id):
            self.asked.append((questions, call_id))
            return {"answers": [{"id": "mode", "selected": ["Default"]}]}

    asker = _Asker()
    agent = AgentLoop(
        model="test", provider=provider, tools=bridge, system_prompt="Test",
    )

    result = await agent.run("ask", callbacks=asker)

    assert result.text == "You chose Default."
    assert result.tool_calls_made == 1
    assert len(asker.asked) == 1
    questions, call_id = asker.asked[0]
    assert questions[0]["id"] == "mode"
    assert call_id == "q1"
    # The tool result the model saw is the JSON answer.
    followup = provider.calls[1][0]
    answer_msgs = [
        m for m in followup
        if m.get("role") == "user" and '"Default"' in m.get("content", "")
    ]
    assert len(answer_msgs) == 1


class _StreamingFakeProvider:
    """Provider that streams a text reply through on_delta in fixed pieces."""

    def __init__(self, pieces: list[str]):
        self.pieces = pieces
        self.saw_on_delta = False

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        if on_delta is not None:
            self.saw_on_delta = True
            for p in self.pieces:
                await on_delta(p)
        return LLMResponse(text="".join(self.pieces), stop_reason="end_turn")

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "user", "content": result.content}

    def max_tokens(self) -> int:
        return 1000


class _RecordingCallbacks(AgentCallbacks):
    def __init__(self):
        self.deltas: list[str] = []
        self.full_texts: list[str] = []

    async def on_text_delta(self, chunk: str) -> None:
        self.deltas.append(chunk)

    async def on_text(self, text: str) -> None:
        # Should NOT be called by the loop — kept to prove no duplicate emission.
        self.full_texts.append(text)


@pytest.mark.asyncio
async def test_streaming_deltas_forwarded_without_duplicate():
    """The loop forwards each provider delta to on_text_delta and does not also
    emit the whole reply via on_text (which would double the text in the UI)."""
    provider = _StreamingFakeProvider(["Hel", "lo, ", "world!"])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(),
        system_prompt="Test",
    )
    cbs = _RecordingCallbacks()

    result = await agent.run("Hi", callbacks=cbs)

    assert provider.saw_on_delta is True  # loop passed on_delta -> streaming path
    assert cbs.deltas == ["Hel", "lo, ", "world!"]
    assert "".join(cbs.deltas) == result.text == "Hello, world!"
    assert cbs.full_texts == []  # no duplicate full-text emission


@pytest.mark.asyncio
async def test_no_callbacks_uses_non_streaming_path():
    """Without callbacks the loop calls chat() without on_delta, so a provider's
    streaming branch is never triggered."""
    provider = _StreamingFakeProvider(["ignored"])
    agent = AgentLoop(
        model="test", provider=provider, tools=ToolBridge(),
        system_prompt="Test",
    )

    result = await agent.run("Hi")  # no callbacks

    assert provider.saw_on_delta is False
    assert result.text == "ignored"


@pytest.mark.asyncio
async def test_subagent_text_streams_through_scoped_callbacks():
    """A subagent's text is forwarded via the callbacks that tracker.scoped()
    hands it, so child deltas reach the UI tagged with the child agent_id."""

    class _StreamingChildProvider:
        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            if on_delta is not None:
                for piece in ["Sub", "agent ", "done"]:
                    await on_delta(piece)
            return LLMResponse(text="Subagent done", stop_reason="end_turn")

        def assistant_message_to_api(self, msg) -> dict:
            return {"role": "assistant", "content": msg.text or ""}

        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": result.content}

        def max_tokens(self) -> int:
            return 1000

    class _ScopedRecorder(AgentCallbacks):
        def __init__(self, agent_id, sink):
            self.agent_id = agent_id
            self.sink = sink

        async def on_text_delta(self, chunk: str) -> None:
            self.sink.append((self.agent_id, chunk))

    class _Tracker:
        def __init__(self):
            self.deltas: list[tuple[str, str]] = []
            self.starts = []
            self.ends = []

        async def on_agent_start(self, agent_id, parent_id, subagent_type, description, depth, prompt=""):
            self.starts.append((agent_id, depth))

        async def on_agent_end(self, agent_id, status, result, input_tokens=0, output_tokens=0):
            self.ends.append((agent_id, status))

        def scoped(self, agent_id, auto_approve=True):
            return _ScopedRecorder(agent_id, self.deltas)

    class _MainProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall(
                    id="tc1", name="task",
                    input={"subagent_type": "explore", "description": "d",
                           "prompt": "go"},
                )], stop_reason="tool_use")
            return LLMResponse(text="parent done", stop_reason="end_turn")

        def assistant_message_to_api(self, msg) -> dict:
            content = []
            if msg.text:
                content.append({"type": "text", "text": msg.text})
            for tc in (msg.tool_calls or []):
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            return {"role": "assistant", "content": content}

        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": result.tool_call_id, "content": result.content,
            }]}

        def max_tokens(self) -> int:
            return 1000

    import os
    tracker = _Tracker()
    builder = AgentBuilder(os.getcwd(), _MainProvider())
    builder.with_default_tools()
    builder.with_subagent_types(["explore"])
    builder.set_tracker(tracker)

    original = builder.build_child

    def patched(subagent_type, task_description, agent_id=""):
        child = original(subagent_type, task_description, agent_id)
        child.provider = _StreamingChildProvider()
        return child

    builder.build_child = patched

    agent = builder.build()
    # Root callbacks: a plain AgentCallbacks (its no-op on_text_delta is fine —
    # we only assert on the child's deltas captured by the scoped recorder).
    result = await agent.run("delegate", callbacks=AgentCallbacks())

    assert result.text == "parent done"
    # The child's text streamed through the scoped recorder, tagged with the
    # child agent_id (not "root").
    child_id = tracker.starts[0][0]
    assert tracker.deltas == [
        (child_id, "Sub"), (child_id, "agent "), (child_id, "done"),
    ]
    assert tracker.ends and tracker.ends[0][1] == "done"


@pytest.mark.asyncio
async def test_tool_end_fires_on_cancellation():
    """When a turn is cancelled while a tool is still running, on_tool_end MUST
    still fire for that call so the UI card resolves (leaves "running" state)."""
    import asyncio
    from cluxmate.tools.base import BaseTool

    class _HangingTool(BaseTool):
        @property
        def name(self) -> str: return "hang"
        @property
        def description(self) -> str: return "Never finishes."
        @property
        def input_schema(self) -> dict:
            return {"type": "object", "properties": {}}
        async def execute(self) -> str:
            await asyncio.sleep(999)
            return "never"

    class _EndRecorder(AgentCallbacks):
        def __init__(self):
            self.ends: list[tuple[str, str]] = []
        async def on_tool_end(self, call_id: str, result) -> None:
            status = "error" if result.is_error else "ok"
            self.ends.append((call_id, status))

    # Provider: issue a hanging tool call, then we cancel the turn.
    class _SingleToolProvider:
        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            return LLMResponse(
                tool_calls=[ToolCall(id="h1", name="hang", input={})],
                stop_reason="tool_use",
            )
        def assistant_message_to_api(self, msg) -> dict:
            return {"role": "assistant", "content": []}
        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": result.content}
        def max_tokens(self) -> int:
            return 1000

    bridge = ToolBridge()
    bridge.register(_HangingTool())
    provider = _SingleToolProvider()
    agent = AgentLoop("test", provider, bridge, system_prompt="Test")
    cbs = _EndRecorder()

    # Run the turn in a task we can cancel mid-tool.
    task = asyncio.create_task(agent.run("go", callbacks=cbs))
    await asyncio.sleep(0.3)  # let on_tool_start fire, tool begin executing
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The hanging tool's on_tool_end must have fired before the cancel
    # propagated, so the UI would see it leave "running".
    assert len(cbs.ends) == 1
    assert cbs.ends[0] == ("h1", "error")  # is_error=True = interrupted


# ── provider-failure fallback ───────────────────────────────────────────────
# Model unavailable / network anomaly → "网络异常，请稍后重试"; quota
# exhaustion → the provider's own message. Both end the turn gracefully
# instead of leaking a raw SDK traceback to whichever front-end is driving.

class _RaisingProvider:
    """Provider whose chat() always raises the given error."""

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
        self.calls += 1
        raise self.error

    def assistant_message_to_api(self, msg) -> dict:
        return {"role": "assistant", "content": msg.text or ""}

    def tool_result_to_api(self, result) -> dict:
        return {"role": "user", "content": result.content}

    def max_tokens(self) -> int:
        return 1000


@pytest.mark.asyncio
async def test_network_error_shows_friendly_fallback():
    from cluxmate.core.providers.base import LLMNetworkError

    agent = AgentLoop(
        "test", _RaisingProvider(LLMNetworkError("Connection refused")),
        ToolBridge(), system_prompt="Test",
    )
    result = await agent.run("Hi")
    assert result.text == NETWORK_FALLBACK_TEXT == "网络异常，请稍后重试"
    # The marker is also the assistant turn in history: roles keep
    # alternating (two consecutive user messages 400 on Anthropic) and the
    # model sees what happened if the conversation continues.
    assert result.history[-1] == {"role": "assistant", "content": result.text}
    assert result.history[-2] == {"role": "user", "content": "Hi"}


@pytest.mark.asyncio
async def test_quota_error_shows_provider_message():
    from cluxmate.core.providers.base import LLMProviderError

    provider_msg = (
        "Access denied, please check your balance. "
        "账户余额不足，请前往平台充值。"
    )
    agent = AgentLoop(
        "test", _RaisingProvider(LLMProviderError(provider_msg)),
        ToolBridge(), system_prompt="Test",
    )
    result = await agent.run("Hi")
    # The provider's own message passes through verbatim — NOT the generic
    # network fallback.
    assert result.text == provider_msg
    assert result.history[-1] == {"role": "assistant", "content": provider_msg}


@pytest.mark.asyncio
async def test_timeout_shows_friendly_fallback():
    """A provider-level timeout is a network anomaly as far as the user is
    concerned — same friendly fallback, no raw '[timed out]' marker."""
    agent = AgentLoop(
        "test", _RaisingProvider(asyncio.TimeoutError("stalled")),
        ToolBridge(), system_prompt="Test",
    )
    result = await agent.run("Hi")
    assert result.text == NETWORK_FALLBACK_TEXT
    assert result.history[-1] == {"role": "assistant", "content": result.text}


@pytest.mark.asyncio
async def test_fallback_mid_conversation_keeps_history_valid():
    """A network failure on a LATER turn still returns the full valid history
    (previous turns + marker), so the desktop persists it and the next send
    resumes cleanly."""
    from cluxmate.core.providers.base import LLMNetworkError

    class _OkThenFailProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, on_delta=None, on_thinking=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(text="first answer", stop_reason="end_turn")
            raise LLMNetworkError("connection reset")

        def assistant_message_to_api(self, msg) -> dict:
            return {"role": "assistant", "content": msg.text or ""}

        def tool_result_to_api(self, result) -> dict:
            return {"role": "user", "content": result.content}

        def max_tokens(self) -> int:
            return 1000

    agent = AgentLoop(
        "test", _OkThenFailProvider(), ToolBridge(), system_prompt="Test",
    )
    r1 = await agent.run("Q1")
    assert r1.text == "first answer"

    r2 = await agent.run("Q2", history=r1.history)
    assert r2.text == NETWORK_FALLBACK_TEXT
    # History: Q1, A1, Q2, fallback-marker — roles alternate throughout.
    roles = [m["role"] for m in r2.history]
    assert roles == ["user", "assistant", "user", "assistant"]
