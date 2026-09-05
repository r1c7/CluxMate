"""Tests for LspTool — schema + operation dispatch."""

import pytest

from cluxmate.tools.lsp_tool import LspTool

_OPERATIONS = [
    "goToDefinition", "goToDeclaration", "goToTypeDefinition", "goToImplementation",
    "findReferences", "hover", "documentSymbol", "workspaceSymbol", "callHierarchy",
    "diagnostics",
]


class _StubManager:
    def __init__(self):
        self.calls = []

    def definition(self, file, line, symbol):
        self.calls.append(("definition", file, line, symbol))
        return "def-result"

    def declaration(self, file, line, symbol):
        self.calls.append(("declaration", file, line, symbol))
        return "decl-result"

    def type_definition(self, file, line, symbol):
        self.calls.append(("type_definition", file, line, symbol))
        return "typedef-result"

    def implementation(self, file, line, symbol):
        self.calls.append(("implementation", file, line, symbol))
        return "impl-result"

    def references(self, file, line, symbol):
        self.calls.append(("references", file, line, symbol))
        return "ref-result"

    def hover(self, file, line, symbol):
        self.calls.append(("hover", file, line, symbol))
        return "hover-result"

    def call_hierarchy(self, file, line, symbol, kind="incomingCalls"):
        self.calls.append(("call_hierarchy", file, line, symbol, kind))
        return f"ch-result:{kind}"

    def document_symbol(self, file):
        self.calls.append(("document_symbol", file))
        return "sym-result"

    def workspace_symbol(self, query):
        self.calls.append(("workspace_symbol", query))
        return "ws-result"

    def diagnostics(self, file):
        self.calls.append(("diagnostics", file))
        return "diag-result"


@pytest.mark.asyncio
async def test_lsp_tool_dispatches_definition():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="goToDefinition", file_path="a.py", line=1, symbol="foo")
    assert out == "def-result"
    assert mgr.calls == [("definition", "a.py", 1, "foo")]


@pytest.mark.asyncio
async def test_lsp_tool_dispatches_new_position_ops():
    mgr = _StubManager()
    tool = LspTool(mgr)
    for op, expected, result in (
        ("goToDeclaration", "declaration", "decl-result"),
        ("goToTypeDefinition", "type_definition", "typedef-result"),
        ("goToImplementation", "implementation", "impl-result"),
    ):
        out = await tool.execute(operation=op, file_path="a.py", line=3, symbol="foo")
        assert out == result
        assert mgr.calls[-1] == (expected, "a.py", 3, "foo")
    assert mgr.calls == [
        ("declaration", "a.py", 3, "foo"),
        ("type_definition", "a.py", 3, "foo"),
        ("implementation", "a.py", 3, "foo"),
    ]


@pytest.mark.asyncio
async def test_lsp_tool_call_hierarchy_kinds():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="callHierarchy", file_path="a.py", line=1, symbol="foo")
    assert out == "ch-result:incomingCalls"
    out = await tool.execute(
        operation="callHierarchy", file_path="a.py", line=1, symbol="foo",
        kind="outgoingCalls",
    )
    assert out == "ch-result:outgoingCalls"
    assert mgr.calls == [
        ("call_hierarchy", "a.py", 1, "foo", "incomingCalls"),
        ("call_hierarchy", "a.py", 1, "foo", "outgoingCalls"),
    ]


@pytest.mark.asyncio
async def test_lsp_tool_call_hierarchy_rejects_unknown_kind():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(
        operation="callHierarchy", file_path="a.py", line=1, symbol="foo", kind="bogus"
    )
    assert "unknown call hierarchy kind" in out
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_lsp_tool_schema_enum_and_safe_risk():
    tool = LspTool(_StubManager())
    assert tool.name == "lsp"
    assert tool.risk_level == "safe"
    op = tool.input_schema["properties"]["operation"]
    assert op["enum"] == _OPERATIONS
    kind = tool.input_schema["properties"]["kind"]
    assert kind["enum"] == ["incomingCalls", "outgoingCalls"]


@pytest.mark.asyncio
async def test_lsp_tool_workspace_symbol_uses_query():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="workspaceSymbol", query="Foo")
    assert out == "ws-result"
    assert mgr.calls == [("workspace_symbol", "Foo")]


@pytest.mark.asyncio
async def test_lsp_tool_dispatches_diagnostics():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="diagnostics", file_path="a.py")
    assert out == "diag-result"
    assert mgr.calls == [("diagnostics", "a.py")]


@pytest.mark.asyncio
async def test_lsp_tool_diagnostics_requires_file_path():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="diagnostics")
    assert "requires file_path" in out
    assert mgr.calls == []
