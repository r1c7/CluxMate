"""Tests for LSPManager.auto_diagnostics — the post-write report (P0-5).

These run against the real fake LSP server (tests/core/fake_lsp_server.py),
which pushes one ERROR and one WARNING for a document containing the marker
"NEEDS_DIAGNOSTICS" — exactly the shape the severity filter must separate.
"""

import sys
from pathlib import Path

import pytest

from cluxmate.core.lsp import LSPManager, ServerSpec

_FAKE_LSP_SERVER = Path(__file__).parent / "fake_lsp_server.py"
MARKER = "NEEDS_DIAGNOSTICS"
QUIET = 0.1  # drain timeout for the "server stays silent" cases


def _spec(**overrides) -> ServerSpec:
    base = dict(
        command=sys.executable,
        args=[str(_FAKE_LSP_SERVER)],
        extension_to_language={".py": "python"},
    )
    base.update(overrides)
    return ServerSpec(**base)


def _manager(tmp_path: Path, spec: ServerSpec | None = None) -> LSPManager:
    return LSPManager(str(tmp_path), specs={"python": spec or _spec()})


def test_reports_errors_only(tmp_path):
    (tmp_path / "a.py").write_text(f"x = 1  # {MARKER}\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        block = mgr.auto_diagnostics("a.py")
    finally:
        mgr.shutdown()
    assert block == (
        '<diagnostics file="a.py">\n'
        "1:1 [error] fake error (fake, E001)\n"
        "</diagnostics>"
    )
    # The severity-2 sibling the fake server also pushed is deliberately dropped.
    assert "fake warning" not in block


def test_clean_file_reports_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        assert mgr.auto_diagnostics("a.py", timeout_seconds=QUIET) == ""
    finally:
        mgr.shutdown()


def test_unknown_language_reports_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        assert mgr.auto_diagnostics("a.txt") == ""
    finally:
        mgr.shutdown()


def test_missing_binary_reports_nothing_and_never_installs(tmp_path, monkeypatch):
    """An edit must never trigger an install, even with auto_install on."""
    (tmp_path / "a.py").write_text(f"x = 1  # {MARKER}\n", encoding="utf-8")
    spec = _spec(
        command="cluxmate-definitely-missing-lsp-xyz",
        install_cmd=["cluxmate-definitely-not-a-real-installer"],
    )
    mgr = _manager(tmp_path, spec)
    mgr.auto_install = True
    called: list = []
    monkeypatch.setattr(LSPManager, "_run_install", lambda self, s: called.append(s) or (False, "boom"))
    try:
        assert mgr.auto_diagnostics("a.py") == ""
    finally:
        mgr.shutdown()
    assert called == []
    assert mgr._clients == {}  # nothing was spawned


def test_reuses_an_already_running_client(tmp_path):
    (tmp_path / "a.py").write_text(f"x = 1  # {MARKER}\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    try:
        mgr.resolve("a.py")  # the explicit `lsp` tool already started the server
        spawned = mgr._clients["python"]
        mgr.auto_diagnostics("a.py")
        assert mgr._clients["python"] is spawned  # not restarted
    finally:
        mgr.shutdown()


def test_malformed_diagnostics_payload_is_ignored(tmp_path):
    """A server's payload shape is not ours to trust."""
    from cluxmate.core.lsp import _format_error_diagnostics

    assert _format_error_diagnostics("a.py", []) == ""
    assert _format_error_diagnostics("a.py", ["not a dict"]) == ""
    assert _format_error_diagnostics("a.py", [{"severity": 1}]) == ""  # no range
    assert _format_error_diagnostics("a.py", [{"range": {}, "severity": 1}]) == ""


def test_error_cap_and_more_suffix():
    from cluxmate.core.lsp import _AUTO_DIAGNOSTICS_MAX, _format_error_diagnostics

    diags = [
        {
            "range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 1}},
            "severity": 1,
            "message": f"e{i}",
        }
        for i in range(_AUTO_DIAGNOSTICS_MAX + 5)
    ]
    block = _format_error_diagnostics("a.py", diags)
    assert block.count("[error]") == _AUTO_DIAGNOSTICS_MAX
    assert "... and 5 more" in block


def test_missing_severity_counts_as_error():
    from cluxmate.core.lsp import _format_error_diagnostics

    block = _format_error_diagnostics("a.py", [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
         "message": "no severity given"},
    ])
    assert "no severity given" in block
