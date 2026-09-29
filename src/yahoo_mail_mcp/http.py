"""Authenticated Streamable HTTP entrypoint for hosted MCP clients.

Supports:
- a legacy static bearer token for operator/debug access
- OAuth 2.1 authorization-code + PKCE for ChatGPT MCP connections
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .app import AppContext
from .server import build_server

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
OAUTH_SCOPE = "mail.read"
AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 30 * 24 * 60 * 60
REFRESH_TOKEN_TTL = 180 * 24 * 60 * 60

_PUBLIC_PATHS = {
    "/health",
    "/authorize",
    "/token",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
}


def _csv_env(name: str) -> set[str]:
    return {
        value.strip().lower().rstrip(".")
        for value in os.environ.get(name, "").split(",")
        if value.strip()
    }


def _allowed_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "testserver"}
    hosts.update(_csv_env("YAHOO_MAIL_MCP_ALLOWED_HOSTS"))
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        hosts.add(railway_domain.lower().rstrip("."))
    return hosts


def _hostname(host_header: str) -> str | None:
    return urlsplit(f"//{host_header}").hostname


def _public_origin() -> str:
    explicit = os.environ.get("YAHOO_MAIL_MCP_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if domain:
        return f"https://{domain}"
    return "http://localhost:8000"


def _resource_id() -> str:
    return f"{_public_origin()}/mcp"


def _oauth_db_path() -> Path:
    value = os.environ.get("YAHOO_MAIL_MCP_OAUTH_DB", "").strip()
    if value:
        return Path(value).expanduser()
    db = os.environ.get("YAHOO_MAIL_MCP_DB", "").strip()
    if db:
        return Path(db).expanduser().with_name("oauth.db")
    return Path.home() / ".yahoo-mail-mcp" / "oauth.db"


def _oauth_conn() -> sqlite3.Connection:
    path = _oauth_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            resource TEXT NOT NULL,
            scope TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            client_id TEXT NOT NULL,
            resource TEXT NOT NULL,
            scope TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        """
    )
    return conn


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_oauth_access_token(token: str) -> bool:
    if not token:
        return False
    now = int(time.time())
    with _oauth_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM oauth_tokens
            WHERE token_hash = ? AND kind = 'access'
              AND resource = ? AND expires_at > ?
            """,
            (_token_hash(token), _resource_id(), now),
        ).fetchone()
    return row is not None


def _is_allowed_chatgpt_client(client_id: str) -> bool:
    if client_id == "https://chatgpt.com/oauth/client.json":
        return True
    return bool(
        re.fullmatch(
            r"https://chatgpt\.com/oauth/[A-Za-z0-9_-]+/client\.json",
            client_id,
        )
    )


def _is_allowed_redirect(uri: str) -> bool:
    if uri == "https://chatgpt.com/connector_platform_oauth_redirect":
        return True
    return bool(
        re.fullmatch(
            r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+",
            uri,
        )
    )


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _validate_authorize_params(params: dict[str, str]) -> str | None:
    if params.get("response_type") != "code":
        return "response_type must be code"
    if not _is_allowed_chatgpt_client(params.get("client_id", "")):
        return "unsupported client_id"
    if not _is_allowed_redirect(params.get("redirect_uri", "")):
        return "unsupported redirect_uri"
    if params.get("code_challenge_method") != "S256":
        return "code_challenge_method must be S256"
    if not params.get("code_challenge"):
        return "code_challenge is required"
    if params.get("resource") != _resource_id():
        return "invalid resource"
    requested = set((params.get("scope") or OAUTH_SCOPE).split())
    if not requested or not requested.issubset({OAUTH_SCOPE}):
        return "unsupported scope"
    return None


def _hidden(name: str, value: str) -> str:
    return (
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
    )


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "transport": "streamable-http"})


async def _protected_resource_metadata(_request: Request) -> JSONResponse:
    origin = _public_origin()
    return JSONResponse(
        {
            "resource": _resource_id(),
            "authorization_servers": [origin],
            "scopes_supported": [OAUTH_SCOPE],
            "resource_documentation": f"{origin}/health",
        }
    )


async def _authorization_server_metadata(_request: Request) -> JSONResponse:
    origin = _public_origin()
    return JSONResponse(
        {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [OAUTH_SCOPE],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": False,
        }
    )


async def _authorize(request: Request):
    if request.method == "GET":
        params = {key: value for key, value in request.query_params.items()}
        error = _validate_authorize_params(params)
        if error:
            return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400)

        password_configured = bool(os.environ.get("YAHOO_MAIL_MCP_OWNER_PASSWORD", ""))
        if not password_configured:
            return HTMLResponse(
                "<h2>Authorization is not configured</h2>"
                "<p>Set YAHOO_MAIL_MCP_OWNER_PASSWORD in Railway first.</p>",
                status_code=503,
            )

        fields = "".join(
            _hidden(name, params.get(name, ""))
            for name in (
                "client_id",
                "redirect_uri",
                "response_type",
                "code_challenge",
                "code_challenge_method",
                "state",
                "resource",
                "scope",
            )
        )
        return HTMLResponse(
            """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize Yahoo Mail MCP</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:520px;margin:48px auto;padding:0 20px}
input,button{font-size:18px;width:100%;box-sizing:border-box;padding:12px;margin-top:10px}
button{font-weight:600} .note{color:#555;line-height:1.45}
</style></head><body>
<h2>Authorize Yahoo Mail MCP</h2>
<p class="note">This grants ChatGPT read-only access to the Yahoo MCP tools hosted in your Railway project.</p>
<form method="post" action="/authorize">"""
            + fields
            + """
<label for="password">Owner password</label>
<input id="password" type="password" name="password" autocomplete="current-password" required>
<button type="submit">Authorize ChatGPT</button>
</form></body></html>"""
        )

    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    params = {key: values[-1] for key, values in parsed.items() if values}
    error = _validate_authorize_params(params)
    if error:
        return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400)

    expected = os.environ.get("YAHOO_MAIL_MCP_OWNER_PASSWORD", "")
    supplied = params.get("password", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        await asyncio.sleep(0.5)
        return HTMLResponse("<h2>Authorization failed</h2><p>Incorrect owner password.</p>", status_code=401)

    code = secrets.token_urlsafe(32)
    now = int(time.time())
    with _oauth_conn() as conn:
        conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ? OR used = 1", (now,))
        conn.execute(
            """
            INSERT INTO oauth_codes
              (code, client_id, redirect_uri, code_challenge, resource, scope, expires_at, used)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                code,
                params["client_id"],
                params["redirect_uri"],
                params["code_challenge"],
                params["resource"],
                params.get("scope") or OAUTH_SCOPE,
                now + AUTH_CODE_TTL,
            ),
        )

    query = {"code": code}
    if params.get("state"):
        query["state"] = params["state"]
    separator = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(params["redirect_uri"] + separator + urlencode(query), status_code=302)


def _issue_tokens(client_id: str, resource: str, scope: str) -> dict:
    access = secrets.token_urlsafe(40)
    refresh = secrets.token_urlsafe(48)
    now = int(time.time())
    with _oauth_conn() as conn:
        conn.execute(
            "INSERT INTO oauth_tokens VALUES (?, 'access', ?, ?, ?, ?)",
            (_token_hash(access), client_id, resource, scope, now + ACCESS_TOKEN_TTL),
        )
        conn.execute(
            "INSERT INTO oauth_tokens VALUES (?, 'refresh', ?, ?, ?, ?)",
            (_token_hash(refresh), client_id, resource, scope, now + REFRESH_TOKEN_TTL),
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


async def _token(request: Request) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    params = {key: values[-1] for key, values in parsed.items() if values}
    grant_type = params.get("grant_type", "")

    if grant_type == "authorization_code":
        code = params.get("code", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        verifier = params.get("code_verifier", "")
        resource = params.get("resource", "")

        now = int(time.time())
        with _oauth_conn() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
            if (
                row is None
                or row["used"]
                or row["expires_at"] <= now
                or row["client_id"] != client_id
                or row["redirect_uri"] != redirect_uri
                or row["resource"] != resource
                or not verifier
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            try:
                challenge = _pkce_s256(verifier)
            except (UnicodeEncodeError, ValueError):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if not hmac.compare_digest(challenge, row["code_challenge"]):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
            scope = row["scope"]

        return JSONResponse(_issue_tokens(client_id, resource, scope))

    if grant_type == "refresh_token":
        refresh = params.get("refresh_token", "")
        client_id = params.get("client_id", "")
        resource = params.get("resource", "")
        now = int(time.time())
        with _oauth_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE token_hash = ? AND kind = 'refresh' AND expires_at > ?
                """,
                (_token_hash(refresh), now),
            ).fetchone()
        if (
            row is None
            or row["client_id"] != client_id
            or row["resource"] != resource
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Rotate the refresh token on every refresh.
        with _oauth_conn() as conn:
            conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (_token_hash(refresh),))
        return JSONResponse(_issue_tokens(client_id, resource, row["scope"]))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


class RemoteSecurityMiddleware:
    """Enforce HTTPS, Host allowlisting, and bearer authentication."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer_token: str,
        allowed_hosts: set[str],
        require_https: bool,
    ):
        self.app = app
        self.bearer_token = bearer_token
        self.allowed_hosts = allowed_hosts
        self.require_https = require_https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        hostname = _hostname(headers.get("host", ""))
        if hostname is None or hostname.lower().rstrip(".") not in self.allowed_hosts:
            await JSONResponse({"error": "Host not allowed"}, status_code=400)(scope, receive, send)
            return

        if self.require_https and path != "/health":
            forwarded_proto = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
            if scope.get("scheme") != "https" and forwarded_proto != "https":
                await JSONResponse({"error": "HTTPS required"}, status_code=400)(scope, receive, send)
                return

        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        scheme, separator, credential = headers.get("authorization", "").partition(" ")
        authenticated = False
        if bool(separator) and scheme.lower() == "bearer":
            authenticated = hmac.compare_digest(credential, self.bearer_token)
            if not authenticated:
                authenticated = _valid_oauth_access_token(credential)

        if not authenticated:
            metadata = f"{_public_origin()}/.well-known/oauth-protected-resource"
            challenge = (
                f'Bearer resource_metadata="{metadata}", '
                f'scope="{OAUTH_SCOPE}"'
            )
            await JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_http_app(
    *,
    ctx: AppContext | None = None,
    bearer_token: str | None = None,
    allowed_hosts: set[str] | None = None,
    require_https: bool | None = None,
) -> Starlette:
    token = bearer_token or os.environ.get("YAHOO_MAIL_MCP_BEARER_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"YAHOO_MAIL_MCP_BEARER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
        )

    app_context = ctx or AppContext()
    app_context.store.interrupt_stale_scan_jobs()
    mcp = build_server(app_context, remote=True)
    app = mcp.streamable_http_app()

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", _protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", _protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _authorization_server_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", _authorization_server_metadata, methods=["GET"]),
        Route("/authorize", _authorize, methods=["GET", "POST"]),
        Route("/token", _token, methods=["POST"]),
    ]
    for route in reversed(routes):
        app.router.routes.insert(0, route)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(starlette_app: Starlette):
        async with original_lifespan(starlette_app):
            try:
                yield
            finally:
                app_context.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(
        RemoteSecurityMiddleware,
        bearer_token=token,
        allowed_hosts=allowed_hosts or _allowed_hosts(),
        require_https=(
            require_https
            if require_https is not None
            else os.environ.get("YAHOO_MAIL_MCP_REQUIRE_HTTPS", "true").lower()
            not in {"0", "false", "no"}
        ),
    )
    return app


def main() -> None:
    level_name = os.environ.get("YAHOO_MAIL_MCP_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    port = int(os.environ.get("PORT", "8000"))
    logger.info("Starting authenticated MCP HTTP server on port %d", port)
    uvicorn.run(
        create_http_app(),
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
