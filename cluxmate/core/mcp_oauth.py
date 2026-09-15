"""MCP OAuth 2.0 client engine (RFC 9728 → RFC 8414 → RFC 7591 → RFC 7636).

Synchronous on purpose: the MCP client is sync (``subprocess``/``httpx.Client``
bridged through ``run_in_executor``) because asyncio transports would bind pipes
to a per-turn event loop that is closed at turn end (see ``core/mcp.py``'s module
docstring). This engine follows the same model and owns its own ``httpx.Client``.

It knows nothing about ``mcp.json`` and never writes to disk: the caller (MCP
client, CLI, JSON-RPC server) supplies an :class:`OAuthFlowConfig` and persists
the returned :class:`OAuthRecord` through :class:`MCPAuthStore`.

Reference implementations this mirrors: Reasonix ``internal/plugin/oauth*.go``
(hand-rolled RFC 9728/8414/7591/7636, the closest architectural match),
Codex ``rmcp-client/src/oauth*.rs``, OpenCode ``mcp/oauth-provider.ts``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from cluxmate.core.mcp_auth_store import (
    OAuthRecord,
    normalize_url,
    origin_of,
    same_origin,
)

DEFAULT_HTTP_TIMEOUT_S = 15.0
DEFAULT_CALLBACK_TIMEOUT_S = 300.0
CALLBACK_PATH = "/oauth/callback"
EXPIRY_SKEW_S = 30.0
CLIENT_NAME = "CluxMate"


@dataclass
class OAuthFlowConfig:
    """Everything the engine needs that comes from configuration."""

    server_name: str
    server_url: str
    client_id: str | None = None
    client_secret: str | None = None
    scopes: str | None = None
    callback_port: int = 0


@dataclass
class Challenge:
    """The resource server's 401 challenge (RFC 9728 / RFC 6750)."""

    resource_metadata: str | None = None
    scope: str | None = None


@dataclass
class OAuthDiscovery:
    resource: str
    as_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes_supported: list[str] = field(default_factory=list)
    auth_methods: list[str] = field(default_factory=list)
    code_challenge_methods: list[str] = field(default_factory=list)


@dataclass
class ClientRegistration:
    client_id: str
    client_secret: str | None = None


class OAuthError(Exception):
    """A failure that is safe to show the user. ``kind`` drives the caller's
    status mapping (needs_auth vs failed); the message goes through ``redact``."""

    def __init__(self, message: str, kind: str = "failed", secrets: list[Any] | None = None):
        self.kind = kind
        super().__init__(redact(message, secrets or []))


def redact(text: str, secrets: list[Any]) -> str:
    """Replace every occurrence of a secret value with ``***``.

    Only ``None`` and ``""`` entries are skipped: every non-empty string is
    redacted however short it is, because a short AS-issued token/secret is
    still a secret that must not reach user-visible text. ``None``/empty are
    skipped because a missing secret would otherwise turn ``replace`` into a
    no-op (or, for ``""``, insert ``***`` between every character).
    """
    out = str(text)
    for s in secrets:
        if isinstance(s, str) and s:
            out = out.replace(s, "***")
    return out


_AUTH_PARAM_RE = re.compile(
    r'(\w+)\s*=\s*"([^"]*)"'          # key="value"
    r'|(\w+)\s*=\s*([^,\s]+)'         # key=value
)


def parse_www_authenticate(value: str) -> Challenge:
    """Parse ``WWW-Authenticate: Bearer resource_metadata="…", scope="…"``.

    Tolerant by design: an unparseable header yields an empty Challenge (the
    caller then falls back to the well-known candidate paths), never an error.
    """
    out = Challenge()
    if not value:
        return out
    for match in _AUTH_PARAM_RE.finditer(value):
        key = match.group(1) or match.group(3)
        val = match.group(2) if match.group(1) else match.group(4)
        if key == "resource_metadata" and out.resource_metadata is None:
            out.resource_metadata = val
        elif key == "scope" and out.scope is None:
            out.scope = val
    return out


# ── PKCE + loopback callback ───────────────────────────────────────────
import http.server
import threading


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    return _b64url(secrets.token_bytes(64))


def code_challenge_s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class _Callback:
    """One-shot loopback listener. ``wait()`` blocks until the browser hits
    CALLBACK_PATH, the deadline passes, or ``cancel()`` is called."""

    requested_port: int = 0
    path: str = CALLBACK_PATH
    timeout: float = DEFAULT_CALLBACK_TIMEOUT_S
    code: str | None = None
    error: str | None = None
    state: str | None = None
    _done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.requested_port), self._handler_cls()
        )
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """The port actually bound — differs from ``requested_port`` when that
        was 0. The caller must use THIS for both the redirect_uri it registers
        and the one it sends on the authorization request."""
        return self._srv.server_address[1]

    def _handler_cls(self):
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer.path:
                    return outer._reply(self, 404, "not found")
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                outer.state = query.get("state")
                outer.error = query.get("error")
                outer.code = query.get("code")
                outer._reply(self, 200, "Authorization complete — you can close this tab.")
                outer._done.set()

            def log_message(self, *a):
                pass

        return _H

    @staticmethod
    def _reply(handler, status: int, text: str) -> None:
        body = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def start(self) -> None:
        self._thread.start()

    def wait(self) -> None:
        self._done.wait(timeout=self.timeout)

    def cancel(self) -> None:
        self._done.set()

    def close(self) -> None:
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:
            pass


class MCPOAuthFlow:
    """One authorization session against one MCP server.

    Not thread-safe by itself; every caller uses a fresh instance (the CLI in
    its own process, the JSON-RPC server per background auth thread).
    """

    def __init__(
        self,
        cfg: OAuthFlowConfig,
        *,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT_S,
        callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT_S,
        open_browser: bool = True,
        on_authorize_url: Callable[[str], None] | None = None,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.http_timeout = http_timeout
        self.callback_timeout = callback_timeout
        self.open_browser = open_browser
        self.on_authorize_url = on_authorize_url
        self.now = now
        self._client = client or httpx.Client(
            timeout=http_timeout, follow_redirects=False
        )
        # Set while authorize() waits for the browser callback; cancel()
        # interrupts it. The annotation is a string because _Callback is defined
        # further down this module (Task 3's half).
        self._callback: "_Callback | None" = None

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _get_json(self, url: str, *, base_origin: str, what: str) -> dict[str, Any]:
        """GET a discovery document. Same-origin only, at most one redirect."""
        resp = self._get(url, base_origin=base_origin, what=what)
        try:
            data = resp.json()
        except ValueError:
            raise OAuthError(f"{what} at {url} is not valid JSON", "discovery")
        if not isinstance(data, dict):
            raise OAuthError(f"{what} at {url} is not a JSON object", "discovery")
        return data

    def _get(self, url: str, *, base_origin: str, what: str) -> httpx.Response:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as e:
            raise OAuthError(f"{what} request failed: {e}", "discovery")
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            target = urllib.parse.urljoin(url, location)
            if not same_origin(target, base_origin):
                raise OAuthError(
                    f"{what} redirected off-origin to {target}", "discovery"
                )
            try:
                resp = self._client.get(target)
            except httpx.HTTPError as e:
                raise OAuthError(f"{what} request failed: {e}", "discovery")
        if resp.status_code != 200:
            raise OAuthError(
                f"{what} request to {url} returned HTTP {resp.status_code}", "discovery"
            )
        return resp

    # ── challenge probe ────────────────────────────────────────────────
    def probe(self) -> Challenge | None:
        """POST a bare ``initialize`` to the MCP endpoint and read the 401
        challenge. Returns None when the server answers without one (healthy, or
        a plain 401 with no WWW-Authenticate)."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "cluxmate", "version": "1.0"}}}
        try:
            resp = self._client.post(
                self.cfg.server_url, json=body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code not in (401, 403):
            return None
        challenge = parse_www_authenticate(resp.headers.get("www-authenticate", ""))
        if challenge.resource_metadata is None and challenge.scope is None:
            return None
        return challenge

    # ── discovery ──────────────────────────────────────────────────────
    def discover(self, challenge: Challenge | None = None) -> OAuthDiscovery:
        """RFC 9728 protected-resource metadata → RFC 8414 / OIDC AS metadata."""
        endpoint_origin = origin_of(self.cfg.server_url)
        if not endpoint_origin:
            raise OAuthError(
                f"server url {self.cfg.server_url!r} has no origin", "discovery"
            )
        prm_candidates: list[str] = []
        if challenge is not None and challenge.resource_metadata:
            if not same_origin(challenge.resource_metadata, self.cfg.server_url):
                raise OAuthError(
                    "resource metadata must be same-origin as the MCP endpoint: "
                    f"{challenge.resource_metadata}",
                    "discovery",
                )
            prm_candidates.append(challenge.resource_metadata)
        parsed = urllib.parse.urlparse(self.cfg.server_url)
        path = parsed.path.rstrip("/")
        prm_candidates.append(f"{endpoint_origin}/.well-known/oauth-protected-resource{path}")
        prm_candidates.append(f"{endpoint_origin}/.well-known/oauth-protected-resource")

        prm: dict[str, Any] | None = None
        last_error: OAuthError | None = None
        for url in prm_candidates:
            try:
                prm = self._get_json(
                    url, base_origin=self.cfg.server_url,
                    what="protected resource metadata",
                )
                break
            except OAuthError as e:
                last_error = e
        if prm is None:
            raise last_error or OAuthError(
                "no protected resource metadata found", "discovery"
            )

        servers = prm.get("authorization_servers")
        issuer = servers[0] if isinstance(servers, list) and servers else endpoint_origin
        if not isinstance(issuer, str) or not issuer:
            raise OAuthError("authorization_servers[0] is missing", "discovery")
        as_url, as_meta = self._fetch_as_metadata(issuer)
        if normalize_url(as_meta.get("issuer") or "") != normalize_url(issuer):
            raise OAuthError(
                f"issuer mismatch: requested {issuer!r}, metadata declared "
                f"{as_meta.get('issuer')!r}",
                "discovery",
            )
        authorization_endpoint = as_meta.get("authorization_endpoint")
        token_endpoint = as_meta.get("token_endpoint")
        if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
            raise OAuthError(
                "authorization/token endpoint missing from AS metadata", "discovery"
            )
        for label, value in (("authorization_endpoint", authorization_endpoint),
                             ("token_endpoint", token_endpoint)):
            if not same_origin(value, as_url):
                raise OAuthError(
                    f"{label} {value} is not same-origin as the AS metadata {as_url}",
                    "discovery",
                )
        registration_endpoint = as_meta.get("registration_endpoint")
        if registration_endpoint is not None:
            if not isinstance(registration_endpoint, str) or not same_origin(
                registration_endpoint, as_url
            ):
                raise OAuthError(
                    f"registration_endpoint {registration_endpoint} is not same-origin "
                    f"as the AS metadata {as_url}",
                    "discovery",
                )
        scopes = as_meta.get("scopes_supported")
        auth_methods = as_meta.get("token_endpoint_auth_methods_supported")
        pkce_methods = as_meta.get("code_challenge_methods_supported")
        return OAuthDiscovery(
            resource=normalize_url(self.cfg.server_url),
            as_url=as_url,
            issuer=as_meta.get("issuer") if isinstance(as_meta.get("issuer"), str) else issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
            scopes_supported=[s for s in scopes if isinstance(s, str)]
            if isinstance(scopes, list) else [],
            auth_methods=[m for m in auth_methods if isinstance(m, str)]
            if isinstance(auth_methods, list) else [],
            code_challenge_methods=[m for m in pkce_methods if isinstance(m, str)]
            if isinstance(pkce_methods, list) else [],
        )

    def _fetch_as_metadata(self, issuer: str) -> tuple[str, dict[str, Any]]:
        """Try RFC 8414 then OIDC well-known paths, with and without the
        issuer's path component (the RFC 8414 §3.1 path-insertion rule)."""
        parsed = urllib.parse.urlparse(issuer)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        candidates = [
            f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/oauth-authorization-server",
            f"{base}/.well-known/openid-configuration{path}",
            f"{base}/.well-known/openid-configuration",
        ]
        last_error: OAuthError | None = None
        for url in candidates:
            try:
                return url, self._get_json(url, base_origin=issuer, what="authorization server metadata")
            except OAuthError as e:
                last_error = e
        raise last_error or OAuthError(
            f"no authorization server metadata at {issuer}", "discovery"
        )

    # ── client registration ────────────────────────────────────────────
    def register_client(self, discovery: OAuthDiscovery,
                        redirect_uri: str | None = None) -> ClientRegistration:
        """Use the configured client, or RFC 7591 dynamic registration.

        ``redirect_uri`` must be the port the callback server is really bound to
        (authorize() passes it); the configured value is only a fallback for a
        caller that has not bound a listener yet.
        """
        if self.cfg.client_id:
            return ClientRegistration(
                client_id=self.cfg.client_id, client_secret=self.cfg.client_secret
            )
        if not discovery.registration_endpoint:
            raise OAuthError(
                "this server does not support dynamic client registration — "
                "set oauth.client_id in mcp.json",
                "registration",
            )
        redirect_uri = redirect_uri or self.callback_redirect()
        payload = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": self._pick_auth_method(discovery),
        }
        if self.cfg.scopes:
            payload["scope"] = self.cfg.scopes
        try:
            resp = self._client.post(discovery.registration_endpoint, json=payload)
        except httpx.HTTPError as e:
            raise OAuthError(f"client registration failed: {e}", "registration")
        if resp.status_code not in (200, 201):
            raise OAuthError(
                f"client registration failed: HTTP {resp.status_code}", "registration"
            )
        try:
            data = resp.json()
        except ValueError:
            raise OAuthError("client registration returned invalid JSON", "registration")
        client_id = data.get("client_id") if isinstance(data, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("client registration returned no client_id", "registration")
        secret = data.get("client_secret")
        return ClientRegistration(
            client_id=client_id, client_secret=secret if isinstance(secret, str) else None
        )

    def _pick_auth_method(self, discovery: OAuthDiscovery) -> str:
        """Prefer the strongest method the AS advertises; default to a public
        client (PKCE) when it advertises none."""
        for method in ("client_secret_basic", "client_secret_post", "none"):
            if method in discovery.auth_methods:
                return method
        return "none"

    def callback_redirect(self, port: int | None = None) -> str:
        """The loopback redirect URI. Always pass the ACTUALLY bound port
        (``_Callback.port``); the configured ``callback_port`` is the fallback,
        and 0 there means "any port", which a strict AS will reject."""
        return f"http://127.0.0.1:{self.cfg.callback_port if port is None else port}{CALLBACK_PATH}"

    # ── interactive authorization ──────────────────────────────────────
    def authorize(self, challenge: Challenge | None = None) -> OAuthRecord:
        """Full flow: bind callback → discover → register → PKCE → browser →
        wait → token exchange.

        Order matters twice over. (1) The listener binds BEFORE discovery and
        registration so the redirect_uri registered with the AS is the port that
        is really listening: with dynamic registration and an ephemeral port
        there is no other way to keep the two in sync, and a strict AS rejects
        the exchange when they differ. (2) The browser opens LAST, after the
        bind, so a fast redirect cannot race it.
        """
        callback = _Callback(
            requested_port=self.cfg.callback_port or 0, timeout=self.callback_timeout
        )
        callback.start()
        self._callback = callback
        redirect_uri = self.callback_redirect(callback.port)
        try:
            discovery = self.discover(challenge)
            registration = self.register_client(discovery, redirect_uri)
            if "S256" not in self._code_challenge_methods(discovery):
                raise OAuthError(
                    "authorization server does not support PKCE S256 — refusing to "
                    "downgrade to plain",
                    "discovery",
                )
            verifier = new_code_verifier()
            state = _b64url(secrets.token_bytes(32))
            url = self._authorization_url(
                discovery, registration, redirect_uri, verifier, state,
                scope=self._resolve_scope(challenge, discovery),
            )
            if self.on_authorize_url is not None:
                self.on_authorize_url(url)
            if self.open_browser:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            callback.wait()
            if callback.code is None and callback.error is None:
                raise OAuthError(
                    f"timed out after {self.callback_timeout:.0f}s waiting for the "
                    f"authorization callback on port {callback.port}",
                    "timeout",
                )
            if callback.error:
                raise OAuthError(
                    f"authorization was denied by the server: {callback.error}", "denied"
                )
            if callback.state != state:
                raise OAuthError(
                    "authorization callback state mismatch — possible CSRF, "
                    "token exchange aborted",
                    "authorize",
                )
            return self._exchange_code(
                discovery, registration, redirect_uri, callback.code, verifier
            )
        finally:
            self._callback = None
            callback.close()

    def _resolve_scope(self, challenge: Challenge | None, discovery: OAuthDiscovery) -> str | None:
        """Challenge scope > configured scopes > AS scopes_supported > none."""
        if challenge is not None and challenge.scope:
            return challenge.scope
        if self.cfg.scopes:
            return self.cfg.scopes
        if discovery.scopes_supported:
            return " ".join(discovery.scopes_supported)
        return None

    def _authorization_url(self, discovery, registration, redirect_uri, verifier, state,
                           scope: str | None) -> str:
        params = {
            "response_type": "code",
            "client_id": registration.client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge_s256(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "resource": discovery.resource,          # RFC 8707
        }
        if scope:
            params["scope"] = scope
        return f"{discovery.authorization_endpoint}?{urllib.parse.urlencode(params)}"

    def _exchange_code(self, discovery, registration, redirect_uri, code, verifier) -> OAuthRecord:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": registration.client_id,
            "code_verifier": verifier,
            "resource": discovery.resource,
        }
        return self._token_request(
            discovery.token_endpoint, form, registration,
            discovery=discovery, error_kind="token",
        )

    def refresh(self, record: OAuthRecord) -> OAuthRecord:
        return refresh_access_token(
            record, client_secret=record.client_secret, http_timeout=self.http_timeout,
            now=self.now,
        )

    def cancel(self) -> None:
        """Interrupt an in-flight authorize() — the `mcp/auth/cancel` path.
        Safe to call at any time; a no-op when nothing is waiting."""
        callback = getattr(self, "_callback", None)
        if callback is not None:
            callback.cancel()

    def _code_challenge_methods(self, discovery: OAuthDiscovery) -> list[str]:
        return discovery.code_challenge_methods or ["S256"]

    def _token_request(self, url, form, registration, *, discovery, error_kind: str) -> OAuthRecord:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        method = self._pick_auth_method(discovery) if discovery is not None else "none"
        if registration.client_secret and method == "client_secret_basic":
            basic = _b64url(f"{registration.client_id}:{registration.client_secret}".encode())
            headers["Authorization"] = f"Basic {basic}"
        elif registration.client_secret and method == "client_secret_post":
            form = {**form, "client_secret": registration.client_secret}
        try:
            resp = self._client.post(url, data=form, headers=headers)
        except httpx.HTTPError as e:
            raise OAuthError(
                f"token request failed: {e}", f"{error_kind}_transient"
                if error_kind == "refresh" else error_kind,
                secrets=[form.get("refresh_token")],
            )
        if resp.status_code != 200:
            body = resp.text[:400]
            kind = f"{error_kind}_transient" if resp.status_code >= 500 else (
                "refresh_rejected" if error_kind == "refresh" else error_kind
            )
            raise OAuthError(
                f"token endpoint returned HTTP {resp.status_code}: {body}", kind,
                secrets=[form.get("refresh_token"), form.get("client_secret")],
            )
        try:
            data = resp.json()
        except ValueError:
            raise OAuthError("token endpoint returned invalid JSON", error_kind,
                             secrets=[form.get("refresh_token")])
        access = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(access, str) or not access:
            raise OAuthError(
                "token response has no access_token", error_kind,
                secrets=[form.get("refresh_token")],
            )
        expires_in = data.get("expires_in")
        expires_at = (
            self.now() + float(expires_in)
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else None
        )
        refresh_token = data.get("refresh_token")
        return OAuthRecord(
            server_url=self.cfg.server_url,
            access_token=access,
            token_endpoint=url,
            issuer=discovery.issuer if discovery is not None else "",
            resource=discovery.resource if discovery is not None else "",
            client_id=registration.client_id,
            client_secret=registration.client_secret,
            refresh_token=refresh_token if isinstance(refresh_token, str)
            else form.get("refresh_token"),
            expires_at=expires_at,
            scope=data.get("scope") if isinstance(data.get("scope"), str)
            else (self.cfg.scopes or ""),
        )


def refresh_access_token(
    record: OAuthRecord,
    *,
    token_endpoint: str | None = None,
    client_secret: str | None = None,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    now: Callable[[], float] = time.time,
) -> OAuthRecord:
    """Exchange a refresh token for a new access token.

    Raises OAuthError with kind ``refresh_rejected`` (the AS definitively
    refused: invalid_grant / invalid_client / 4xx) or ``refresh_transient``
    (timeout / 5xx / connection error). Callers delete stored credentials on the
    former and keep them on the latter.
    """
    url = token_endpoint or record.token_endpoint
    if not record.refresh_token:
        raise OAuthError("no refresh token stored", "refresh_rejected")
    if not url:
        raise OAuthError("no token endpoint recorded", "refresh_rejected")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": record.refresh_token,
        "client_id": record.client_id,
    }
    secret = client_secret if client_secret is not None else record.client_secret
    if secret:
        form["client_secret"] = secret
    if record.resource:
        form["resource"] = record.resource        # RFC 8707, same as the code exchange
    with httpx.Client(timeout=http_timeout, follow_redirects=False) as client:
        try:
            resp = client.post(
                url, data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as e:
            raise OAuthError(f"token refresh failed: {e}", "refresh_transient",
                             secrets=[record.refresh_token, secret])
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            raise OAuthError("token refresh returned invalid JSON", "refresh_transient",
                             secrets=[record.refresh_token])
        access = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(access, str) or not access:
            raise OAuthError("token refresh returned no access_token", "refresh_transient",
                             secrets=[record.refresh_token])
        expires_in = data.get("expires_in")
        new_refresh = data.get("refresh_token")
        return OAuthRecord(
            server_url=record.server_url,
            access_token=access,
            token_endpoint=url,
            issuer=record.issuer,
            resource=record.resource,
            client_id=record.client_id,
            client_secret=secret,
            refresh_token=new_refresh if isinstance(new_refresh, str) else record.refresh_token,
            expires_at=(now() + float(expires_in))
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else record.expires_at,
            scope=data.get("scope") if isinstance(data.get("scope"), str) else record.scope,
        )
    kind = "refresh_transient" if resp.status_code >= 500 else "refresh_rejected"
    raise OAuthError(
        f"token refresh returned HTTP {resp.status_code}: {resp.text[:400]}", kind,
        secrets=[record.refresh_token, secret],
    )
