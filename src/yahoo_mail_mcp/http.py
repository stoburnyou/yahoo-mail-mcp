"""Authenticated Streamable HTTP entrypoint for hosted MCP clients."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .app import AppContext
from .server import build_server

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32


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
        if path == "/health":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        hostname = _hostname(headers.get("host", ""))
        if hostname is None or hostname.lower().rstrip(".") not in self.allowed_hosts:
            await JSONResponse({"error": "Host not allowed"}, status_code=400)(scope, receive, send)
            return

        if self.require_https:
            forwarded_proto = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
            if scope.get("scheme") != "https" and forwarded_proto != "https":
                await JSONResponse({"error": "HTTPS required"}, status_code=400)(
                    scope, receive, send
                )
                return

        scheme, separator, credential = headers.get("authorization", "").partition(" ")
        authenticated = (
            bool(separator)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(credential, self.bearer_token)
        )
        if not authenticated:
            await JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "transport": "streamable-http"})


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
    app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))

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
