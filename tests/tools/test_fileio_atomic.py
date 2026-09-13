"""write_preserving is atomic and preserves the target's mode."""

import os
from pathlib import Path

from cluxmate.tools._fileio import write_preserving


def test_write_leaves_no_temp_and_keeps_mode(tmp_path):
    p = tmp_path / "run.sh"
    p.write_text("old", encoding="utf-8")
    if os.name != "nt":
        os.chmod(p, 0o755)

    write_preserving(p, "new\n", "\n")

    assert p.read_text(encoding="utf-8") == "new\n"
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o755
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "run.sh"]
    assert leftovers == []


def test_crlf_conversion_still_applies(tmp_path):
    p = tmp_path / "win.txt"
    p.write_bytes(b"a\r\n")
    write_preserving(p, "a\nb\n", "\r\n")
    assert p.read_bytes() == b"a\r\nb\r\n"
