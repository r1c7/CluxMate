"""Tests for MultiEditTool."""
import pytest
from cluxmate.tools.multi_edit import MultiEditTool


@pytest.mark.asyncio
async def test_all_succeed(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("hello world\n")
    b = tmp_path / "b.txt"
    b.write_text("foo bar\n")

    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(edits=[
        {"path": "a.txt", "old_string": "hello", "new_string": "hi"},
        {"path": "b.txt", "old_string": "foo", "new_string": "baz"},
    ])

    assert "Applied 2/2" in result
    assert "✓ a.txt" in result
    assert "✓ b.txt" in result
    assert a.read_text() == "hi world\n"
    assert b.read_text() == "baz bar\n"


@pytest.mark.asyncio
async def test_partial_failure(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("hello world\n")

    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(edits=[
        {"path": "a.txt", "old_string": "hello", "new_string": "hi"},
        {"path": "a.txt", "old_string": "NONEXISTENT", "new_string": "x"},
    ])

    assert "Applied 1/2" in result
    assert "✗" in result
    assert a.read_text() == "hi world\n"


@pytest.mark.asyncio
async def test_selected_filter(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("hello\n")
    b = tmp_path / "b.txt"
    b.write_text("world\n")

    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(
        edits=[
            {"path": "a.txt", "old_string": "hello", "new_string": "hi"},
            {"path": "b.txt", "old_string": "world", "new_string": "earth"},
        ],
        _selected=[1],
    )

    assert "Applied 1/1" in result
    assert "✓ b.txt" in result
    assert a.read_text() == "hello\n"
    assert b.read_text() == "earth\n"


@pytest.mark.asyncio
async def test_empty_edits(tmp_path):
    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(edits=[])
    assert "Error" in result


@pytest.mark.asyncio
async def test_file_not_found(tmp_path):
    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(edits=[
        {"path": "nonexistent.txt", "old_string": "x", "new_string": "y"},
    ])
    assert "Applied 0/1" in result
    assert "✗" in result


@pytest.mark.asyncio
async def test_preserves_crlf(tmp_path):
    a = tmp_path / "a.txt"
    a.write_bytes(b"hello\r\nworld\r\n")

    tool = MultiEditTool(workdir=str(tmp_path))
    await tool.execute(edits=[
        {"path": "a.txt", "old_string": "hello", "new_string": "hi"},
    ])

    assert a.read_bytes() == b"hi\r\nworld\r\n"


@pytest.mark.asyncio
async def test_out_of_range_index(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("hello\n")

    tool = MultiEditTool(workdir=str(tmp_path))
    result = await tool.execute(
        edits=[
            {"path": "a.txt", "old_string": "hello", "new_string": "hi"},
        ],
        _selected=[0, 5],
    )

    assert "Applied 1/2" in result
    assert "out of range" in result
