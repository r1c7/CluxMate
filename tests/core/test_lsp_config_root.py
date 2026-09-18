"""`lsp.json` follows the CONFIG ROOT; the LSP *workspace* stays on the session tree.

Phase 1A split "the project's config state" from "the tree the session may
write": every project-config reader takes ``config_root`` = PROJECT ROOT (the git
main worktree when the session runs inside a linked worktree), while the
writable/execution tree stays the session cwd. ``lsp.json`` was the one reader
left behind, and the two halves are easy to conflate because ``LSPManager`` uses
its first positional argument for BOTH:

- the project half of ``LSPConfigManager._roots()`` must be read from
  ``config_root``;
- ``LSPManager.ws_root`` must NOT move — it is a protocol/tree concept feeding
  ``LSPClient(root=…)``, the server's spawn cwd, ``initialize``'s ``rootUri``,
  the installer's cwd and ``_abs``/``_display_path``.

Every test pins both halves so reverting either one fails, and the fallback
(no ``config_root``) is pinned too: the positional ``LSPManager(...)`` /
``LSPConfigManager(...)`` call sites across the suite (8 in ``test_lsp.py``)
must keep reading the session tree byte-identically.
"""

from __future__ import annotations

import json
from pathlib import Path

from cluxmate.core.builder import AgentBuilder
from cluxmate.core.lsp import LSPConfigManager, LSPManager


class _Provider:
    def set_reasoning_effort(self, effort):
        pass


# The main repo declares a custom python server and opts into auto-install; the
# worktree ships a DIFFERENT set, so whichever file is read is unmistakable.
_MAIN_LSP = {"auto_install": True, "servers": {"python": {"command": "fake-lsp"}}}
_TREE_LSP = {"auto_install": False, "servers": {"python": {"command": "wt-lsp"}}}


def _home(monkeypatch, tmp_path) -> Path:
    """Redirect the global config root so a real ~/.cluxmate cannot leak in."""
    home = tmp_path / "home"
    (home / ".cluxmate").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _repo_and_worktree(tmp_path) -> tuple[Path, Path]:
    """A main repo and a session worktree inside it."""
    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "me"
    (repo / ".cluxmate").mkdir(parents=True)
    wt.mkdir(parents=True)
    return repo, wt


def _write_lsp(root: Path, data: dict) -> None:
    (root / ".cluxmate").mkdir(parents=True, exist_ok=True)
    (root / ".cluxmate" / "lsp.json").write_text(json.dumps(data), encoding="utf-8")


# ── case 1: the project half comes from the config root ─────────────────────

def test_config_manager_reads_the_project_half_from_config_root(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)
    _write_lsp(wt, _TREE_LSP)

    cfg = LSPConfigManager(str(wt), config_root=str(repo)).load_config()

    assert cfg.specs["python"].command == "fake-lsp"
    assert cfg.auto_install is True


def test_lsp_manager_reads_specs_and_auto_install_from_the_config_root(
    tmp_path, monkeypatch
):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)
    _write_lsp(wt, _TREE_LSP)

    mgr = LSPManager(str(wt), config_root=str(repo))
    try:
        assert mgr.specs["python"].command == "fake-lsp"
        # auto_install is the top-level flag of the SAME file: the worktree's
        # `false` must not win, or a worktree session could silence the main
        # repo's opt-in (or, worse, a worktree file could switch it on).
        assert mgr._auto_install_default is True
    finally:
        mgr.shutdown()


# ── case 2: the protocol/tree root does NOT move ────────────────────────────

def test_workspace_root_and_relative_paths_stay_on_the_session_tree(
    tmp_path, monkeypatch
):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)

    mgr = LSPManager(str(wt), config_root=str(repo))
    try:
        assert mgr.ws_root == str(wt)
        # _abs is the tree resolver behind LSPClient(root=…), rootUri, the
        # server's spawn cwd and the installer's cwd — all still the session tree.
        assert mgr._abs("pkg/a.py") == str(wt / "pkg" / "a.py")
    finally:
        mgr.shutdown()


# ── case 3: the fallback — no config_root means today's behavior ────────────

def test_without_a_config_root_the_session_tree_is_read(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)
    _write_lsp(wt, _TREE_LSP)

    for mgr in (LSPManager(str(wt)), LSPManager(str(wt), config_root=None)):
        try:
            assert mgr.specs["python"].command == "wt-lsp"
            assert mgr._auto_install_default is False
        finally:
            mgr.shutdown()


# ── case 4: the trust gate is unchanged ─────────────────────────────────────

def test_untrusted_config_root_contributes_no_project_lsp_json(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)
    _write_lsp(home, {"servers": {"python": {"install_hint": "global hint"}}})

    mgr = LSPManager(str(wt), config_root=str(repo), trusted=False)
    try:
        # trust gates the PROJECT half only: the global file still applies…
        assert mgr.specs["python"].install_hint == "global hint"
        # …while the main repo's server and its auto_install opt-in are withheld.
        assert mgr.specs["python"].command == "pyright-langserver"
        assert mgr._auto_install_default is False
    finally:
        mgr.shutdown()


# ── case 5: the builder hands both roots to the one construction site ───────

def test_builder_wires_the_config_root_into_the_lsp_manager(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    repo, wt = _repo_and_worktree(tmp_path)
    _write_lsp(repo, _MAIN_LSP)
    _write_lsp(wt, _TREE_LSP)

    b = AgentBuilder(str(wt), _Provider()).with_project_root(str(repo))
    try:
        mgr = b._lsp_manager()
        # config from the main repo, workspace on the writable session tree.
        assert mgr.specs["python"].command == "fake-lsp"
        assert mgr.ws_root == str(wt)
    finally:
        b.lsp_shutdown()
