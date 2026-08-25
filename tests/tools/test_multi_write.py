"""Tests for MultiWriteTool."""
import pytest
from cluxmate.tools.multi_write import MultiWriteTool


@pytest.mark.asyncio
async def test_creates_multiple_files(tmp_path):
    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(files=[
        {"path": "a.txt", "content": "AAA\n"},
        {"path": "sub/b.txt", "content": "BBB\n"},
    ])

    assert "Wrote 2/2" in result
    assert (tmp_path / "a.txt").read_text() == "AAA\n"
    # Parent directories are created automatically.
    assert (tmp_path / "sub" / "b.txt").read_text() == "BBB\n"


@pytest.mark.asyncio
async def test_overwrites_existing(tmp_path):
    existing = tmp_path / "a.txt"
    existing.write_text("old\n")

    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(files=[{"path": "a.txt", "content": "new\n"}])

    assert "overwrote" in result
    assert existing.read_text() == "new\n"


@pytest.mark.asyncio
async def test_selected_filters_indices(tmp_path):
    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(
        files=[
            {"path": "a.txt", "content": "A\n"},
            {"path": "b.txt", "content": "B\n"},
            {"path": "c.txt", "content": "C\n"},
        ],
        _selected=[0, 2],
    )

    assert "Wrote 2/2" in result
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert (tmp_path / "c.txt").exists()


@pytest.mark.asyncio
async def test_empty_files_errors(tmp_path):
    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(files=[])
    assert "Error" in result


@pytest.mark.asyncio
async def test_directory_path_fails_gracefully(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()

    tool = MultiWriteTool(workdir=str(tmp_path))
    result = await tool.execute(files=[
        {"path": "adir", "content": "x"},
        {"path": "ok.txt", "content": "ok\n"},
    ])

    # One failure doesn't block the rest.
    assert "Wrote 1/2" in result
    assert "is a directory" in result
    assert (tmp_path / "ok.txt").read_text() == "ok\n"


@pytest.mark.asyncio
async def test_crlf_preserved_on_overwrite(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes(b"line1\r\nline2\r\n")

    tool = MultiWriteTool(workdir=str(tmp_path))
    await tool.execute(files=[{"path": "win.txt", "content": "a\nb\n"}])

    # Existing CRLF style is preserved rather than flipped to LF.
    assert f.read_bytes() == b"a\r\nb\r\n"
