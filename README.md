# yahoo-mail-mcp

[![CI](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml)

An MCP server for auditing and safely cleaning up large Yahoo Mail backlogs.
It runs locally over stdio or as an authenticated Streamable HTTP service for
clients such as Notion Custom Agents. It scans header metadata only, keeps
constant memory usage, resumes interrupted scans, groups mail by exact sender
domain, and separates review from mailbox mutation.

Message bodies and attachments are not downloaded or indexed.

## Why this exists

Large Yahoo mailboxes can contain hundreds of thousands of messages while the
standard IMAP view exposes only a limited window. This project handles Yahoo's
`UIDONLY`, `PARTIAL`, and `MESSAGELIMIT` extensions internally so an MCP client
can perform one resumable scan instead of thousands of tool round trips.

The intended workflow is:

1. Connect with a Yahoo app password.
2. Scan message headers into local SQLite.
3. Browse recent mail, search headers, or review sender-domain summaries.
4. Explicitly tag domains as Keep, Unsubscribe, Archive, Delete, or Needs Review.
5. Preview the exact cleanup snapshot.
6. Execute approved decisions; archives move out of Inbox and deletions move
   to Trash without expunging.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A Yahoo app password for each account
- An MCP client such as Cursor, Claude Desktop, or a Notion Custom Agent

Generate app passwords from Yahoo Account Security. Never use your regular
Yahoo password in this project.

## Install from source

```bash
git clone https://github.com/ktrann24/yahoo-mail-mcp.git
cd yahoo-mail-mcp
uv sync
cp .env.example .env
chmod 600 .env
```

Edit `.env` and replace the placeholders:

```env
YAHOO_ACCOUNTS='[{"name":"personal","email":"you@yahoo.com","app_password":"your-app-password"}]'
```

Multiple account objects can be included in the JSON array. Account `name`
values must be unique.

When a package release is published, the server will also be runnable as:

```bash
uvx yahoo-mail-mcp
```

## Register with Cursor

Copy `.cursor/mcp.json.example` to `.cursor/mcp.json`, replace the placeholder
with the absolute path to your clone, and restart Cursor:

```bash
cp .cursor/mcp.json.example .cursor/mcp.json
```

The real `.cursor/mcp.json` is ignored because it contains a machine-specific
absolute path.

## Deploy to Railway for Notion

Railway is used instead of Vercel because this server needs a durable SQLite
file and long-running background scans. Vercel Functions have ephemeral local
storage and cannot run a durable worker.

1. Push or fork this repository and create a Railway service from it.
2. Add a Railway volume mounted at `/data`.
3. Generate a bearer token locally with `openssl rand -hex 32`.
4. Set these Railway variables:

```env
YAHOO_ACCOUNTS=[{"name":"personal","email":"you@yahoo.com","app_password":"your-app-password"}]
YAHOO_MAIL_MCP_DB=/data/mail.db
YAHOO_MAIL_MCP_BEARER_TOKEN=<output-from-openssl>
YAHOO_MAIL_MCP_REQUIRE_HTTPS=true
```

5. Generate a Railway public domain. The server automatically allows the
   hostname in Railway's `RAILWAY_PUBLIC_DOMAIN` variable.
6. Have a workspace admin enable custom MCP servers under **Settings → Notion
   AI → AI connectors**. In the agent's **Settings → Tools & Access**, choose
   **Add connection → Custom MCP server**, enter
   `https://<your-domain>/mcp`, and configure header-based bearer-token
   authentication with the same token.

`GET /health` is public and contains no account data. Every MCP request requires
the bearer token, an allowed Host header, and HTTPS. Keep the Railway service
private except for its generated HTTPS domain, and never put the token or Yahoo
app passwords in source control.

The remote server omits the CSV import/export tools because accepting arbitrary
server filesystem paths over the network is unsafe. All other tools use the
same implementation as local mode.

## Read-only tools

- `list_accounts`: verify authentication and list folders and counts.
- `scan_mailbox`: run a checkpointed, header-only historical or incremental scan.
- `start_scan_job`, `get_scan_job`, and `list_scan_jobs`: run and monitor a
  durable background scan without holding a remote MCP request open.
- `get_scan_status`: inspect checkpoints without connecting to IMAP.
- `list_recent_messages`: browse cached recent headers with privacy-conscious defaults.
- `search_messages`: search cached subject and sender metadata with filters.
- `get_message_headers`: inspect one cached account/folder/UID reference.
- `list_sender_groups` and `get_sender_detail`: review aggregate sender-domain activity.
- `triage_new_mail`: incrementally scan new mail and return advisory suggestions.

List and search results hide full sender email addresses unless explicitly
requested. Raw unsubscribe URLs and tokens are never returned by browse tools.

## Review and cleanup tools

- `set_decisions`: tag domains, including Archive, without taking mailbox action.
- `export_review_csv` and `import_review_csv`: spreadsheet review round trip.
- `preview_cleanup`: calculate exact affected counts and issue a short-lived token.
- `execute_decisions`: move approved mail to Archive or Trash, or execute
  supported unsubscribe methods.

Tools publish MCP safety annotations. Cached reads are marked read-only,
`set_decisions` is a non-destructive write, and `execute_decisions` is marked
destructive so clients such as Notion can request user confirmation. MCP
annotations are advisory client metadata, not an authorization boundary; the
server still applies its own preview and large-operation safeguards.

## Safety model

- Scans open folders read-only and use `BODY.PEEK` for selected header fields.
- Scan limits apply across the whole tool call and checkpoints resume safely.
- UIDVALIDITY changes invalidate stale folder data.
- Deletes use `UID MOVE` to Trash, never permanent expunge.
- Archives use `UID MOVE` to the provider's `\Archive` folder.
- Large Archive and Delete moves require a 15-minute, single-use token bound
  to the exact message snapshot.
- The target folder, UIDVALIDITY, and live sender domain are checked before moves.
- One-click unsubscribe requires HTTPS, a related sender domain, public resolved
  addresses, a pinned connection target, and no redirects.
- Plain web unsubscribe links are reported for manual action.
- Destructive actions are written to the local audit log.

Start with `max_messages=100` on one folder. Review the cached data before
tagging or executing any decision.

## Configuration

- `YAHOO_ACCOUNTS` (required): JSON array of account names, emails, and app passwords.
- `YAHOO_MAIL_MCP_DB`: SQLite path; defaults to `~/.yahoo-mail-mcp/mail.db`.
- `YAHOO_MAIL_MCP_DELETE_THRESHOLD`: messages above which a confirm token is
  required for Archive or Delete; defaults to `1000`.
- `YAHOO_MAIL_MCP_BATCH_SIZE`: IMAP fetch size; defaults to `500` and is capped
  below Yahoo's advertised `MESSAGELIMIT`.
- `YAHOO_MAIL_MCP_LOG_LEVEL`: Python log level; defaults to `WARNING`.
- `YAHOO_MAIL_MCP_BEARER_TOKEN`: required in HTTP mode; at least 32 characters.
- `YAHOO_MAIL_MCP_ALLOWED_HOSTS`: optional comma-separated custom hostnames.
  Railway's public domain is allowed automatically.
- `YAHOO_MAIL_MCP_REQUIRE_HTTPS`: defaults to `true` in HTTP mode.

## Privacy and local data

The SQLite database stores account aliases, folders, UIDs, sender names and
addresses, subjects, dates, sizes, unsubscribe headers, decisions,
checkpoints, and action logs. It does not store message bodies or attachments.

The database directory is created with owner-only permissions on POSIX
systems. Use `YAHOO_MAIL_MCP_DB` to place it on an encrypted volume if needed.
CSV exports contain mail metadata and should be protected like the database.
MCP clients may retain tool output and stderr logs according to their own
policies.

In hosted mode, the database and Yahoo app passwords live in the hosting
account instead of on your computer. Secure the Railway account with MFA,
restrict project access, retain the persistent volume, and rotate both the
bearer token and Yahoo app passwords if either may have been exposed.

To remove the local index after stopping the server:

```bash
rm -rf ~/.yahoo-mail-mcp
```

This removes only the local index. It does not modify Yahoo Mail.

## Development

All fixtures must be synthetic; never commit real message headers or mailbox
exports.

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy src/yahoo_mail_mcp
uv run pytest -q
uv build
uv run twine check dist/*
```

## Security

See [SECURITY.md](SECURITY.md) for private vulnerability reporting. Do not
publish credentials, mailbox data, or unsubscribe tokens in issues.

## License

MIT. See [LICENSE](LICENSE).
