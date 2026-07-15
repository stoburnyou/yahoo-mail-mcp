#!/bin/sh
set -eu

db_dir="$(dirname "${YAHOO_MAIL_MCP_DB:-/data/mail.db}")"
mkdir -p "$db_dir"

if [ "$(id -u)" = "0" ]; then
    chown app:app "$db_dir"
    chmod 0700 "$db_dir"
    exec gosu app "$@"
fi

exec "$@"
