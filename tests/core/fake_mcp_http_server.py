"""A local MCP-over-HTTP server + OAuth 2.0 authorization server for tests.

Run by the pytest suite in-process (``FakeOAuthServer.start()`` binds an
ephemeral port on 127.0.0.1). Every knob below breaks exactly one thing, so a
test can assert that CluxMate refuses what it must refuse.

Knobs (constructor kwargs):
    require_bearer=True        → /mcp 401s without a valid Bearer
    challenge=True             → the 401 carries a WWW-Authenticate header
    resource_metadata_url=…    → override the advertised metadata URL (to test
                                 the cross-origin rejection)
    issuer_mismatch=False      → AS metadata declares a different issuer
    registration=False         → drop the registration endpoint (no DCR)
    auth_methods=["none"]      → token_endpoint_auth_methods_supported
    token_scope="read write"   → scope advertised in the challenge
    access_token="at-1"        → the token /token hands out
    token_status=200           → make /token fail with this status
    token_error_body={"error":"invalid_grant"} → body for a failing /token
    authorize_redirect_state=False → 302 with a wrong state (CSRF test)
    authorize_error=None       → "access_denied" to simulate a user refusal
    pkce_methods=["S256"]      → code_challenge_methods_supported (["plain"] to refuse S256)
    endpoint_origins={name: origin} → point one AS metadata endpoint
                               (authorization_endpoint / token_endpoint /
                               registration_endpoint) at another origin
    redirect_prm_to="http://…" → 302 the protected-resource metadata elsewhere
    serve_oas=True             → False 404s the oauth-authorization-server path
                                 and serves the metadata at openid-configuration
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import urllib.parse


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── helpers ────────────────────────────────────────────────────────
    @property
    def k(self) -> dict:
        return self.server.knobs  # type: ignore[attr-defined]

    @property
    def log(self) -> list:
        return self.server.requests  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def log_message(self, *a):  # keep pytest output clean
        pass

    # ── MCP resource server ────────────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        raw = self._read_body()
        self.log.append({"method": "POST", "path": path,
                         "headers": dict(self.headers), "body": raw.decode("utf-8")})
        if path == "/token":
            return self._token(raw)
        if path == "/register":
            return self._register(raw)
        if path == "/mcp":
            return self._mcp(raw)
        self._empty(404)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.log.append({"method": "GET", "path": path,
                         "headers": dict(self.headers), "query": query})
        if path == "/authorize":
            return self._authorize(query)
        if path == "/.well-known/oauth-protected-resource/mcp" or \
           path == "/.well-known/oauth-protected-resource":
            if self.k["redirect_prm_to"]:
                return self._empty(302, {"Location": self.k["redirect_prm_to"]})
            return self._prm()
        if path == "/.well-known/openid-configuration":
            return self._as_metadata()
        if path == "/.well-known/oauth-authorization-server":
            if not self.k["serve_oas"]:
                return self._empty(404)
            return self._as_metadata()
        self._empty(404)

    def _origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _mcp(self, raw: bytes) -> None:
        auth = self.headers.get("Authorization") or ""
        if self.k["require_bearer"] and auth != f"Bearer {self.k['access_token']}":
            headers = {}
            if self.k["challenge"]:
                prm = self.k["resource_metadata_url"] or (
                    f"{self._origin()}/.well-known/oauth-protected-resource/mcp"
                )
                headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{prm}", '
                    f'scope="{self.k["token_scope"]}"'
                )
            return self._empty(401, headers)
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._json(400, {"error": "bad json"})
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {},
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "echo",
                                 "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}]}
        else:
            return self._json(200, {"jsonrpc": "2.0", "id": req.get("id"),
                                    "error": {"code": -32601, "message": "no method"}})
        return self._json(200, {"jsonrpc": "2.0", "id": req.get("id"), "result": result})

    # ── OAuth authorization server ─────────────────────────────────────
    def _prm(self) -> None:
        self._json(200, {
            "resource": f"{self._origin()}/mcp",
            "authorization_servers": [self._origin()],
            "scopes_supported": ["read", "write"],
        })

    def _as_metadata(self) -> None:
        issuer = self.k["issuer_mismatch"] and f"{self._origin()}/other" or self._origin()
        payload = {
            "issuer": issuer,
            "authorization_endpoint": self._endpoint("authorization_endpoint", "/authorize"),
            "token_endpoint": self._endpoint("token_endpoint", "/token"),
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": list(self.k["pkce_methods"]),
            "scopes_supported": ["read", "write"],
            "token_endpoint_auth_methods_supported": list(self.k["auth_methods"]),
        }
        if self.k["registration"]:
            payload["registration_endpoint"] = self._endpoint(
                "registration_endpoint", "/register"
            )
        self._json(200, payload)

    def _endpoint(self, name: str, suffix: str) -> str:
        """This origin's endpoint, or another origin's when the knob says so."""
        override = (self.k["endpoint_origins"] or {}).get(name)
        return f"{override}{suffix}" if override else f"{self._origin()}{suffix}"

    def _register(self, raw: bytes) -> None:
        body = json.loads(raw.decode("utf-8"))
        self.server.registrations.append(body)  # type: ignore[attr-defined]
        self._json(201, {"client_id": "dcr-client", "client_secret": "dcr-secret",
                         "redirect_uris": body.get("redirect_uris", [])})

    def _authorize(self, query: dict) -> None:
        redirect_uri = (query.get("redirect_uri") or [""])[0]
        state = (query.get("state") or [""])[0]
        self.server.authorize_query = query  # type: ignore[attr-defined]
        if self.k["authorize_error"]:
            loc = f"{redirect_uri}?error={self.k['authorize_error']}&state={state}"
            return self._empty(302, {"Location": loc})
        code = "code-1"
        self.server.codes[code] = {  # type: ignore[attr-defined]
            "challenge": (query.get("code_challenge") or [""])[0],
            "redirect_uri": redirect_uri,
            "resource": (query.get("resource") or [None])[0],
        }
        out_state = "wrong-state" if self.k["authorize_redirect_state"] else state
        self._empty(302, {"Location": f"{redirect_uri}?code={code}&state={out_state}"})

    def _token(self, raw: bytes) -> None:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
        self.server.token_forms.append(form)  # type: ignore[attr-defined]
        if self.k["token_status"] != 200:
            return self._json(self.k["token_status"], self.k["token_error_body"])
        grant = form.get("grant_type")
        if grant == "authorization_code":
            rec = self.server.codes.get(form.get("code", ""))  # type: ignore[attr-defined]
            if rec is None:
                return self._json(400, {"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            if not verifier or s256(verifier) != rec["challenge"]:
                return self._json(400, {"error": "invalid_grant",
                                        "error_description": "pkce mismatch"})
        elif grant == "refresh_token":
            if form.get("refresh_token") != self.k["refresh_token"]:
                return self._json(400, {"error": "invalid_grant"})
        else:
            return self._json(400, {"error": "unsupported_grant_type"})
        self._json(200, {
            "access_token": self.k["access_token"],
            "token_type": "Bearer",
            "expires_in": self.k["expires_in"],
            "refresh_token": self.k["refresh_token"],
            "scope": self.k["token_scope"],
        })


class FakeOAuthServer:
    """Started/stopped by a pytest fixture. ``base_url`` is the MCP endpoint's
    origin; the MCP endpoint itself is ``base_url + '/mcp'``."""

    DEFAULTS = dict(
        require_bearer=True, challenge=True, resource_metadata_url=None,
        issuer_mismatch=False, registration=True, auth_methods=["none"],
        token_scope="read write", access_token="at-1", refresh_token="rt-1",
        expires_in=3600, token_status=200, token_error_body={"error": "invalid_grant"},
        authorize_redirect_state=False, authorize_error=None, pkce_methods=["S256"],
        endpoint_origins=None, redirect_prm_to=None, serve_oas=True,
    )

    def __init__(self, **over):
        # Copy list-valued defaults per instance so one test can never mutate
        # (and so contaminate) another test's server.
        knobs = {k: (list(v) if isinstance(v, list) else v)
                 for k, v in self.DEFAULTS.items()}
        knobs.update(over)
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.knobs = knobs
        self._srv.requests = []
        self._srv.registrations = []
        self._srv.authorize_query = None
        self._srv.codes = {}
        self._srv.token_forms = []
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def token_forms(self) -> list[dict]:
        return self._srv.token_forms

    @property
    def registrations(self) -> list[dict]:
        return self._srv.registrations

    @property
    def authorize_query(self) -> dict | None:
        return self._srv.authorize_query

    @property
    def requests(self) -> list[dict]:
        return self._srv.requests

    def paths(self) -> list[str]:
        return [r["path"] for r in self._srv.requests]


def serve_from_thread(url: str) -> None:
    """Simulate a browser: GET the authorization URL once (the fake AS answers
    with a 302 to the loopback callback). Used to patch ``webbrowser.open``."""
    import urllib.request

    def _go():
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pass  # the 302 target is the callback; a redirect loop is fine here

    threading.Thread(target=_go, daemon=True).start()
