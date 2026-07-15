FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    YAHOO_MAIL_MCP_DB="/data/mail.db"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY docker-entrypoint.sh /usr/local/bin/yahoo-mail-mcp-entrypoint
RUN chmod 0755 /usr/local/bin/yahoo-mail-mcp-entrypoint \
    && mkdir -p /data \
    && chown -R app:app /app /data

EXPOSE 8000
ENTRYPOINT ["yahoo-mail-mcp-entrypoint"]
CMD ["yahoo-mail-mcp-http"]
