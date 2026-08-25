"""Shared file I/O helpers that preserve a file's original newline style.

The problem: `Path.read_text()` / `write_text()` on Windows translate newlines
via the platform default. Reading normalizes CRLF→LF (universal newlines), and
writing turns every LF back into CRLF (os.linesep). So editing a single line of
an LF file rewrites *every* line ending to CRLF — a huge phantom diff that
pollutes git and the "changed files" card.

These helpers read raw bytes (so the original newline style is visible), expose
LF-normalized text for matching, and write back through byte I/O with the
original style restored — no implicit platform translation either way.
"""

from pathlib import Path


def detect_newline(raw_text: str) -> str:
    """Return the dominant newline style ('\\r\\n' or '\\n') in raw text.

    `raw_text` must be the UNnormalized decoded content (CRLF intact), i.e.
    read via read_bytes().decode(), not Path.read_text().
    """
    crlf = raw_text.count("\r\n")
    lone_lf = raw_text.count("\n") - crlf
    return "\r\n" if crlf > lone_lf else "\n"


def read_normalized(path: Path) -> tuple[str, str]:
    """Read a file, returning (LF-normalized text, original newline style).

    Reads bytes and decodes as UTF-8 so the original CRLF/LF is observable,
    then normalizes to LF for string matching. The returned newline style is
    fed back to write_preserving() so edits don't flip line endings.
    """
    raw = path.read_bytes().decode("utf-8")
    newline = detect_newline(raw)
    normalized = raw.replace("\r\n", "\n")
    return normalized, newline


def write_preserving(path: Path, text: str, newline: str) -> None:
    """Write LF-based text to a file using the given newline style.

    Writes bytes directly (never text mode) so Python performs no newline
    translation of its own — the output is exactly what we intend.
    """
    if newline == "\r\n":
        # Collapse any stray CRLF first so we never double to \r\r\n.
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
