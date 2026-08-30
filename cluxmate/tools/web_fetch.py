"""Web fetch tool — make HTTP requests with streaming byte cap."""

import json
from typing import Any

import httpx

from .base import BaseTool

from ._ssrf import SSRFBlockedError, validate_url

# Hard byte cap on response download — prevents memory exhaustion on large responses.
# Applied during streaming (connection aborted if exceeded), before the global
# MAX_OUTPUT_CHARS truncation in BaseTool.run_safe.
MAX_RESPONSE_BYTES = 512_000  # 512 KB

# Headers allowed in verbose filtered mode (lowercase keys)
_SAFE_HEADERS = {"content-type", "content-length", "last-modified"}

# Methods considered read-only for plan-mode restriction
_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


class WebFetchTool(BaseTool):
    """Fetch a URL and return its content with configurable format and limits."""

    def __init__(self, plan_mode: bool = False, ssrf: "SsrConfig | None" = None):
        self.plan_mode = plan_mode
        self._ssrf = ssrf

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL and return its content. "
            "Supports GET, POST, PUT, DELETE, PATCH, HEAD with custom headers and body. "
            "Returns raw text with status metadata or verbose JSON."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP method",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "Custom request headers as key-value pairs",
                    "default": {},
                },
                "body": {
                    "type": "string",
                    "description": "Request body (for POST/PUT/PATCH)",
                    "default": "",
                },
                "format": {
                    "type": "string",
                    "description": "Output format: 'raw' for text with status prefix, 'verbose' for JSON",
                    "enum": ["raw", "verbose"],
                    "default": "raw",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Total timeout in milliseconds",
                    "default": 30000,
                    "minimum": 1000,
                    "maximum": 120000,
                },
                "include_all_headers": {
                    "type": "boolean",
                    "description": "In verbose mode, return all response headers instead of filtered subset",
                    "default": False,
                },
            },
            "required": ["url"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    async def execute(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        format: str = "raw",
        timeout_ms: int = 30000,
        include_all_headers: bool = False,
    ) -> str:
        # Plan-mode enforcement: reject write methods at execution level
        if self.plan_mode and method.upper() not in _READ_ONLY_METHODS:
            return (
                f"Error: Method '{method}' is not allowed in read-only mode. "
                f"Allowed methods: {', '.join(sorted(_READ_ONLY_METHODS))}"
            )

        # SSRF guard: refuse internal/private destinations before connecting.
        err = self._check_ssrf(url)
        if err:
            return f"Error: {err}"

        method = method.upper()
        headers = headers or {}
        body = body or None

        # Compute split timeouts from total timeout_ms
        total_s = timeout_ms / 1000.0
        connect_timeout = max(5.0, total_s / 3.0)
        read_timeout = max(10.0, total_s)
        write_timeout = read_timeout

        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=5.0,
        )

        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            event_hooks={"request": [self._ssrf_hook]},
        ) as client:
            try:
                async with client.stream(
                    method, url, headers=headers, content=body
                ) as response:
                    # Read body in chunks, counting bytes
                    chunks: list[bytes] = []
                    bytes_read = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        chunk_len = len(chunk)
                        if bytes_read + chunk_len > MAX_RESPONSE_BYTES:
                            remaining = MAX_RESPONSE_BYTES - bytes_read
                            if remaining > 0:
                                chunks.append(chunk[:remaining])
                            truncated = True
                            break
                        chunks.append(chunk)
                        bytes_read += chunk_len

                    raw_body = b"".join(chunks).decode(
                        response.encoding or "utf-8", errors="replace"
                    )

                    if format == "verbose":
                        return self._format_verbose(
                            response, raw_body, include_all_headers, truncated
                        )
                    else:
                        return self._format_raw(response, raw_body, truncated)

            except SSRFBlockedError as e:
                return f"Error: {e}"
            except httpx.TimeoutException:
                return f"Error: Request timed out after {timeout_ms}ms"
            except httpx.ConnectError as e:
                return f"Error: Connection failed — {e}"
            except httpx.HTTPError as e:
                return f"Error: HTTP request failed — {e}"

    def _format_raw(
        self,
        response: httpx.Response,
        body: str,
        truncated: bool,
    ) -> str:
        ct = response.headers.get("content-type", "")
        lines = [
            f"Status: {response.status_code} {response.reason_phrase}",
            f"Content-Type: {ct}",
            "",
            body,
        ]
        result = "\n".join(lines)
        if truncated:
            result += f"\n[Content truncated at {MAX_RESPONSE_BYTES} byte read limit]"
        return result

    def _format_verbose(
        self,
        response: httpx.Response,
        body: str,
        include_all_headers: bool,
        truncated: bool,
    ) -> str:
        if include_all_headers:
            headers = dict(response.headers)
        else:
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() in _SAFE_HEADERS
            }

        result = {
            "status": response.status_code,
            "status_text": response.reason_phrase,
            "headers": headers,
            "redirect_url": str(response.url),
            "body": body,
        }
        raw = json.dumps(result, ensure_ascii=False, indent=2)
        if truncated:
            raw += f"\n[Content truncated at {MAX_RESPONSE_BYTES} byte read limit]"
        return raw

    def _check_ssrf(self, url: str) -> str | None:
        """Error message if *url* is blocked by the SSRF guard, else None."""
        if self._ssrf is None:
            return validate_url(url)
        cfg = self._ssrf.snapshot()
        return validate_url(url, cfg["allow"], cfg["block_extra"])

    async def _ssrf_hook(self, request: httpx.Request) -> None:
        """httpx request hook — runs before EVERY request, including each
        redirect hop of follow_redirects. Must be async: AsyncClient awaits
        every event hook (httpx >= 0.28)."""
        err = self._check_ssrf(str(request.url))
        if err:
            raise SSRFBlockedError(err)
