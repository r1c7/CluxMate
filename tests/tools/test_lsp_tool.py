"""Tests for LspTool — schema + operation dispatch."""

import asyncio

import pytest

from cluxmate.tools.lsp_tool import LspTool


class _StubManager:
    def __init__(self):
        self.calls = []

    def definition(self, file, line, symbol):
        self.calls.append(("definition", file, line, symbol))
        return "def-result"

    def references(self, file, line, symbol):
        self.calls.append(("references", file, line, symbol))
        return "ref-result"

    def hover(self, file, line, symbol):
        self.calls.append(("hover", file, line, symbol))
        return "hover-result"

    def document_symbol(self, file):
        self.calls.append(("document_symbol", file))
        return "sym-result"

    def workspace_symbol(self, query):
        self.calls.append(("workspace_symbol", query))
        return "ws-result"


@pytest.mark.asyncio
async def test_lsp_tool_dispatches_definition():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="goToDefinition", file_path="a.py", line=1, symbol="foo")
    assert out == "def-result"
    assert mgr.calls == [("definition", "a.py", 1, "foo")]


@pytest.mark.asyncio
async def test_lsp_tool_schema_enum_and_safe_risk():
    tool = LspTool(_StubManager())
    assert tool.name == "lsp"
    assert tool.risk_level == "safe"
    op = tool.input_schema["properties"]["operation"]
    assert op["enum"] == [
        "goToDefinition", "findReferences", "hover", "documentSymbol", "workspaceSymbol",
    ]


@pytest.mark.asyncio
async def test_lsp_tool_workspace_symbol_uses_query():
    mgr = _StubManager()
    tool = LspTool(mgr)
    out = await tool.execute(operation="workspaceSymbol", query="Foo")
    assert out == "ws-result"
    assert mgr.calls == [("workspace_symbol", "Foo")]
