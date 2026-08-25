"""Web search tool — search the web via DuckDuckGo Lite and return results."""

import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from .base import BaseTool

# Patterns to extract results from DuckDuckGo Lite HTML. Lite uses a simple
# table layout; patterns handle both single- and double-quoted attributes so
# minor upstream changes don't silently return zero results.
_RESULT_LINK_RE = re.compile(
    r"""<a\s[^>]*href="(https?://[^"]+)"[^>]*class=['"]result-link['"][^>]*>(.*?)</a>""",
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r"""<td\s+class=['"]result-snippet['"][^>]*>(.*?)</td>""",
    re.DOTALL | re.IGNORECASE,
)
# Fallback: any link with result-link class (looser pattern)
_RESULT_LINK_LOOSE_RE = re.compile(
    r"""<a\s[^>]*href="(https?://[^"]+)"[^>]*>.*?</a>""",
    re.DOTALL | re.IGNORECASE,
)

_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
_TIMEOUT = 20.0

_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#x27|#39);")


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from *text*."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
    return text.strip()


def _parse_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Extract (title, url, snippet) tuples from DuckDuckGo Lite HTML."""
    results: list[dict[str, str]] = []

    links = _RESULT_LINK_RE.findall(html)
    snippets = _SNIPPET_RE.findall(html)

    for i, (url, title) in enumerate(links):
        if len(results) >= max_results:
            break
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        results.append({
            "title": _strip_html(title),
            "url": url,
            "snippet": snippet,
        })

    return results


class WebSearchTool(BaseTool):
    """Search the web and return results with titles, URLs, and snippets."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web and return results with titles, URLs, and snippets. "
            "Use for finding current documentation, error messages, API references, "
            "or any information beyond your knowledge cutoff."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-20, default 10).",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    async def execute(
        self,
        query: str,
        max_results: int = 10,
    ) -> str:
        max_results = max(1, min(max_results, 20))

        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "CluxMate/1.0"},
        ) as client:
            try:
                # DuckDuckGo Lite uses POST for search (the form is method="post").
                response = await client.post(
                    _SEARCH_URL,
                    data={"q": query, "kl": ""},
                )
                response.raise_for_status()
            except httpx.TimeoutException:
                return f"Error: Search request timed out for query: {query}"
            except httpx.HTTPError as e:
                return f"Error: Search request failed — {e}"

        results = _parse_results(response.text, max_results)

        if not results:
            return f"No results found for query: {query}"

        lines = [f"Search results for: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   {r['url']}")
            if r["snippet"]:
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines).strip()
