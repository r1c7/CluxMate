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
