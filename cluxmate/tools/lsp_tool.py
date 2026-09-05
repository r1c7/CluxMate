"""LspTool — a single read-only tool exposing ten LSP navigation operations."""

import asyncio
from typing import Any

from .base import BaseTool

_OPERATIONS = [
    "goToDefinition", "goToDeclaration", "goToTypeDefinition", "goToImplementation",
    "findReferences", "hover", "documentSymbol", "workspaceSymbol", "callHierarchy",
    "diagnostics",
]

_CALL_KINDS = ("incomingCalls", "outgoingCalls")

_POSITION_OPS = {
    "goToDefinition", "goToDeclaration", "goToTypeDefinition", "goToImplementation",
    "findReferences", "hover",
}


class LspTool(BaseTool):
    """Query a language server for precise code navigation."""

    def __init__(self, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "lsp"

    @property
    def description(self) -> str:
        return (
            "Query a language server for precise code navigation. operation is one of "
            "goToDefinition, goToDeclaration, goToTypeDefinition, goToImplementation, "
            "findReferences, hover, documentSymbol, workspaceSymbol, callHierarchy, "
            "diagnostics. "
            "For goToDefinition/goToDeclaration/goToTypeDefinition/goToImplementation/"
            "findReferences/hover, pass file_path (relative to the workspace or "
            "absolute), the 1-based line, and the exact symbol text on that line "
            "(the column is located internally). callHierarchy takes the same plus "
            "kind (incomingCalls or outgoingCalls, default incomingCalls). "
            "documentSymbol takes file_path only. workspaceSymbol takes query only. "
            "diagnostics takes file_path only. "
            "If the language server is not installed, the tool reports how to "
            "install it (or installs it automatically when auto_install is enabled "
            "in lsp.json)."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(_OPERATIONS),
                    "description": "Which navigation query to run.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Source file, relative to the workspace or absolute.",
                },
                "line": {
                    "type": "integer",
                    "description": "1-based line number the symbol appears on.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Exact symbol text on that line, e.g. \"executeBatch\".",
                },
                "query": {
                    "type": "string",
                    "description": "Search string for workspaceSymbol.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(_CALL_KINDS),
                    "description": "For callHierarchy: who calls this symbol "
                                   "(incomingCalls) or what it calls (outgoingCalls). "
                                   "Defaults to incomingCalls.",
                },
            },
            "required": ["operation"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    async def execute(self, operation: str, file_path: str | None = None,
                      line: int | None = None, symbol: str | None = None,
                      query: str | None = None, kind: str | None = None) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._run, operation, file_path, line, symbol, query, kind
        )

    def _run(self, operation: str, file_path: str | None, line: int | None,
             symbol: str | None, query: str | None, kind: str | None) -> str:
        if operation in _POSITION_OPS:
            if not file_path or line is None or not symbol:
                return "Error: operation requires file_path, line, and symbol"
            if line < 1:
                return "Error: line must be >= 1"
            fn = {
                "goToDefinition": self._manager.definition,
                "goToDeclaration": self._manager.declaration,
                "goToTypeDefinition": self._manager.type_definition,
                "goToImplementation": self._manager.implementation,
                "findReferences": self._manager.references,
                "hover": self._manager.hover,
            }[operation]
            return fn(file_path, line, symbol)
        if operation == "callHierarchy":
            if not file_path or line is None or not symbol:
                return "Error: callHierarchy requires file_path, line, and symbol"
            if line < 1:
                return "Error: line must be >= 1"
            if kind is not None and kind not in _CALL_KINDS:
                return f"Error: unknown call hierarchy kind {kind!r} (use incomingCalls or outgoingCalls)"
            return self._manager.call_hierarchy(
                file_path, line, symbol, kind or "incomingCalls"
            )
        if operation == "diagnostics":
            if not file_path:
                return "Error: diagnostics requires file_path"
            return self._manager.diagnostics(file_path)
        if operation == "documentSymbol":
            if not file_path:
                return "Error: documentSymbol requires file_path"
            return self._manager.document_symbol(file_path)
        if operation == "workspaceSymbol":
            if not query:
                return "Error: workspaceSymbol requires query"
            return self._manager.workspace_symbol(query)
        return f"Error: unknown operation {operation!r}"
