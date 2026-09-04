"""Tests for TodoTool — model-declared whole-list task tracking."""

import pytest

from cluxmate.tools.todo import TodoTool, canonical_todos


def test_risk_level_is_safe():
    assert TodoTool().risk_level == "safe"


def test_session_event_declared():
    assert TodoTool.session_event == "todo/write"


def test_schema_advertises_statuses():
    schema = TodoTool().input_schema
    props = schema["properties"]["todos"]["items"]["properties"]
    assert set(props["status"]["enum"]) == {"pending", "in_progress", "completed"}
    assert schema["required"] == ["todos"]


@pytest.mark.asyncio
async def test_execute_counts():
    tool = TodoTool()
    out = await tool.execute(
        todos=[
            {"content": "a", "status": "pending"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "completed"},
            {"content": "d", "status": "completed"},
        ]
    )
    assert "1 pending" in out
    assert "1 in progress" in out
    assert "2 completed" in out


@pytest.mark.asyncio
async def test_execute_allows_parallel_in_progress():
    # CluxMate executes tool calls concurrently (asyncio.gather), so several
    # todos may be in_progress at once.
    tool = TodoTool()
    out = await tool.execute(
        todos=[
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )
    assert "2 in progress" in out


@pytest.mark.asyncio
async def test_execute_empty_list_is_valid():
    out = await TodoTool().execute(todos=[])
    assert "0 pending" in out


@pytest.mark.asyncio
async def test_execute_trims_content():
    tool = TodoTool()
    out = await tool.execute(todos=[{"content": "  x  ", "status": "completed"}])
    assert "1 completed" in out
    assert tool.result_data(
        {"todos": [{"content": "  x  ", "status": "completed"}]}, out
    ) == {"todos": [{"content": "x", "status": "completed"}]}


@pytest.mark.asyncio
async def test_execute_empty_content_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(todos=[{"content": "   ", "status": "pending"}])


@pytest.mark.asyncio
async def test_execute_non_string_content_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(todos=[{"content": 42, "status": "pending"}])


@pytest.mark.asyncio
async def test_execute_duplicate_content_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(
            todos=[
                {"content": "same", "status": "pending"},
                {"content": "same", "status": "completed"},
            ]
        )


@pytest.mark.asyncio
async def test_execute_bad_status_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(todos=[{"content": "x", "status": "done"}])


@pytest.mark.asyncio
async def test_execute_non_list_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(todos={"content": "x", "status": "pending"})


@pytest.mark.asyncio
async def test_execute_bad_item_shape_is_error():
    with pytest.raises(ValueError):
        await TodoTool().execute(todos=["just a string"])


@pytest.mark.asyncio
async def test_run_safe_carries_canonical_data():
    tool = TodoTool()
    result = await tool.run_safe(
        "call-1", todos=[{"content": " x ", "status": "in_progress"}]
    )
    assert not result.is_error
    assert result.data == {"todos": [{"content": "x", "status": "in_progress"}]}


@pytest.mark.asyncio
async def test_run_safe_error_result_has_no_data():
    tool = TodoTool()
    result = await tool.run_safe(
        "call-1", todos=[{"content": "  ", "status": "pending"}]
    )
    assert result.is_error
    assert result.data is None


def test_canonical_todos_does_not_mutate_input():
    raw = [{"content": " x ", "status": "pending"}]
    canonical_todos(raw)
    assert raw == [{"content": " x ", "status": "pending"}]
