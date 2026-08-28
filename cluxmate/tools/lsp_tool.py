"""LspTool — a single read-only tool exposing five LSP navigation operations."""

import asyncio
from typing import Any

from .base import BaseTool


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
            "goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol. "
            "For goToDefinition/findReferences/hover, pass file_path (relative to the "
            "workspace or absolute), the 1-based line, and the exact symbol text on that "
            "line (the column is located internally). documentSymbol takes file_path only. "
            "workspaceSymbol takes query only."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "goToDefinition", "findReferences", "hover",
                        "documentSymbol", "workspaceSymbol",
                    ],
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
            },
            "required": ["operation"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    async def execute(self, operation: str, file_path: str | None = None,
                      line: int | None = None, symbol: str | None = None,
                      query: str | None = None) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._run, operation, file_path, line, symbol, query
        )

    def _run(self, operation: str, file_path: str | None, line: int | None,
             symbol: str | None, query: str | None) -> str:
        if operation in ("goToDefinition", "findReferences", "hover"):
            if not file_path or line is None or not symbol:
                return "Error: operation requires file_path, line, and symbol"
            if line < 1:
                return "Error: line must be >= 1"
            fn = {
                "goToDefinition": self._manager.definition,
                "findReferences": self._manager.references,
                "hover": self._manager.hover,
            }[operation]
            return fn(file_path, line, symbol)
        if operation == "documentSymbol":
            if not file_path:
                return "Error: documentSymbol requires file_path"
            return self._manager.document_symbol(file_path)
        if operation == "workspaceSymbol":
            if not query:
                return "Error: workspaceSymbol requires query"
            return self._manager.workspace_symbol(query)
        return f"Error: unknown operation {operation!r}"
