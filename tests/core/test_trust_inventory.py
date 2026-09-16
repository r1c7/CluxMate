"""Inventory guard: every gated reader is built with an explicit ``trusted=``.

``trusted`` is a keyword-only argument that each project-config reader opts into
(``cluxmate/core/trust.py`` holds the registry behind it), so the failure this
guards against is a *new* construction site — usually a tool or a CLI path
rather than the builder — that quietly keeps the default ``trusted=True`` and
therefore loads whatever an untrusted repository ships.

The walk is source-level and deliberately so: importing or instantiating a
reader would execute project config, which is the thing under test. ``ast`` (not
a regex over raw text) finds ``Call`` nodes, so a class name inside a comment, a
docstring or a string literal is not mistaken for a construction, and every
failure names the offending ``file:line`` plus the enclosing scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cluxmate

# The readers whose project-level config is gated on a trust decision: each one
# takes ``trusted: bool = True`` as a keyword-only argument. A name only belongs
# here if it really has that parameter — ``test_every_gated_name_...`` below
# enforces it against the source, so the list cannot silently rot.
GATED_READERS = frozenset(
    {
        "HookManager",
        "LSPConfigManager",
        "LSPManager",
        "MCPConfigManager",
        "MCPManager",
        "PermissionPolicy",
        "RetrievalMemory",
        "SkillManager",
        "SubagentRegistry",
    }
)

# Constructions of a gated reader that intentionally omit ``trusted=``. Keyed by
# (package-relative path, enclosing scope, reader) rather than by line number,
# so unrelated drift does not turn the guard off. An empty mapping is the goal.
_EXEMPT: dict[tuple[str, str, str], str] = {
    (
        "core/jsonrpc_server.py",
        "JsonRpcServer.__init__",
        "PermissionPolicy",
    ): (
        "placeholder: bound to the process cwd and unconditionally rebuilt with "
        "the resolved trust by initialize() (jsonrpc_server.py:855) before the "
        "bridge serves a turn. Exempt rather than trusted= so the guard stays "
        "strict about readers that make their own trust decision. Recorded as a "
        "residual gap in the fix-wave report."
    ),
}


def _package_root() -> Path:
    """The ``cluxmate`` package directory as imported — the installed/editable
    package, i.e. this repository's source tree for a dev checkout."""
    return Path(cluxmate.__file__).resolve().parent


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _calls_with_scope(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    """Every ``Call`` node, paired with the dotted name of its enclosing
    class/function scope (``"<module>"`` at the top level)."""
    found: list[tuple[ast.Call, str]] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, scope + (child.name,))
                continue
            if isinstance(child, ast.Call):
                found.append((child, ".".join(scope) or "<module>"))
            walk(child, scope)

    walk(tree, ())
    return found


def _callee(node: ast.Call) -> str | None:
    """The constructed name: ``Foo(...)`` and ``mod.Foo(...)`` both yield
    ``"Foo"``; anything else (a call on a call, a subscript) yields ``None``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_every_gated_name_in_the_inventory_is_a_trust_gated_reader():
    root = _package_root()
    classes: dict[str, tuple[str, ast.ClassDef]] = {}
    for path in _sources(root):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, (rel, node))

    problems: list[str] = []
    for name in sorted(GATED_READERS):
        where = classes.get(name)
        if where is None:
            problems.append(f"{name}: no class of that name anywhere in the package")
            continue
        rel, cls = where
        init = next(
            (
                n
                for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"
            ),
            None,
        )
        if init is None:
            problems.append(f"{name} ({rel}:{cls.lineno}): no __init__ to gate")
            continue
        if not any(a.arg == "trusted" for a in init.args.kwonlyargs):
            problems.append(
                f"{name} ({rel}:{init.lineno}): __init__ has no keyword-only `trusted`"
            )
            continue
        default = next(
            (
                d
                for a, d in zip(init.args.kwonlyargs, init.args.kw_defaults)
                if a.arg == "trusted"
            ),
            None,
        )
        if not (isinstance(default, ast.Constant) and default.value is True):
            problems.append(
                f"{name} ({rel}:{init.lineno}): `trusted` does not default to True"
            )

    assert not problems, (
        "The gated-reader inventory no longer matches the source; either the "
        "reader's signature changed or the name does not belong here:\n"
        + "\n".join(f"  {p}" for p in problems)
    )


def test_no_gated_reader_is_constructed_without_an_explicit_trust_flag():
    root = _package_root()
    offenders: list[str] = []
    for path in _sources(root):
        rel = path.relative_to(root).as_posix()
        for call, scope in _calls_with_scope(_parse(path)):
            reader = _callee(call)
            if reader not in GATED_READERS:
                continue
            if any(kw.arg == "trusted" for kw in call.keywords):
                continue
            if (rel, scope, reader) in _EXEMPT:
                continue
            splats = [kw.value for kw in call.keywords if kw.arg is None]
            why = (
                f"passes **{ast.unparse(splats[0])}, so trusted= cannot be seen here"
                if splats
                else "no trusted= argument"
            )
            offenders.append(
                f"  cluxmate/{rel}:{call.lineno}  {reader}(...) in {scope} — {why}"
            )

    assert not offenders, (
        "A gated project-config reader was constructed without an explicit "
        "trusted= argument, so it reads the repository's config even when the "
        "directory is untrusted:\n"
        + "\n".join(offenders)
        + "\n\nPass trusted=<bool> at the call site, or — only if the reader is "
        "genuinely built before a trust decision exists — add a commented entry "
        "to _EXEMPT in this file."
    )
