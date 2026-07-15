# yahoo-mail-mcp

[![CI](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml)

A local MCP server for auditing and safely cleaning up large Yahoo Mail
backlogs. It scans header metadata only, keeps constant memory usage, resumes
interrupted scans, groups mail by exact sender domain, and separates review
from destructive execution.

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
4. Explicitly tag domains as Keep, Unsubscribe, Delete, or Needs Review.
5. Preview the exact cleanup snapshot.
6. Execute approved decisions; deletions move mail to Trash and never expunge.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A Yahoo app password for each account
- A local MCP client such as Cursor or Claude Desktop

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

## Read-only tools

- `list_accounts`: verify authentication and list folders and counts.
- `scan_mailbox`: run a checkpointed, header-only historical or incremental scan.
- `get_scan_status`: inspect checkpoints without connecting to IMAP.
- `list_recent_messages`: browse cached recent headers with privacy-conscious defaults.
- `search_messages`: search cached subject and sender metadata with filters.
- `get_message_headers`: inspect one cached account/folder/UID reference.
- `list_sender_groups` and `get_sender_detail`: review aggregate sender-domain activity.
- `triage_new_mail`: incrementally scan new mail and return advisory suggestions.

List and search results hide full sender email addresses unless explicitly
requested. Raw unsubscribe URLs and tokens are never returned by browse tools.

## Review and cleanup tools

- `set_decisions`: tag domains without taking mailbox action.
- `export_review_csv` and `import_review_csv`: spreadsheet review round trip.
- `preview_cleanup`: calculate exact affected counts and issue a short-lived token.
- `execute_decisions`: move approved mail to Trash or execute supported unsubscribe methods.

## Safety model

- Scans open folders read-only and use `BODY.PEEK` for selected header fields.
- Scan limits apply across the whole tool call and checkpoints resume safely.
- UIDVALIDITY changes invalidate stale folder data.
- Deletes use `UID MOVE` to Trash, never permanent expunge.
- Large deletes require a 15-minute, single-use token bound to the exact message snapshot.
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
- `YAHOO_MAIL_MCP_DELETE_THRESHOLD`: messages above which a delete token is
  required; defaults to `1000`.
- `YAHOO_MAIL_MCP_BATCH_SIZE`: IMAP fetch size; defaults to `500` and is capped
  below Yahoo's advertised `MESSAGELIMIT`.
- `YAHOO_MAIL_MCP_LOG_LEVEL`: Python log level; defaults to `WARNING`.

## Privacy and local data

The SQLite database stores account aliases, folders, UIDs, sender names and
addresses, subjects, dates, sizes, unsubscribe headers, decisions,
checkpoints, and action logs. It does not store message bodies or attachments.

The database directory is created with owner-only permissions on POSIX
systems. Use `YAHOO_MAIL_MCP_DB` to place it on an encrypted volume if needed.
CSV exports contain mail metadata and should be protected like the database.
MCP clients may retain tool output and stderr logs according to their own
policies.

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
