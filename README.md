# yahoo-mail-mcp

[![CI](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ktrann24/yahoo-mail-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)

Safely audit and clean up large Yahoo Mail backlogs from any MCP client.
`yahoo-mail-mcp` scans header metadata, groups mail by account and sender
domain, and keeps review separate from mailbox mutation.

> [!WARNING]
> This is alpha, single-owner software. Start with a small scan and review every
> Archive, Delete, or Unsubscribe action before approving it.

Message bodies and attachments are never downloaded or indexed.

## What it does

- Scans very large Yahoo mailboxes without downloading message bodies.
- Resumes interrupted historical scans and incrementally picks up new mail.
- Browses and searches cached sender, subject, date, size, and unsubscribe data.
- Lists one-click, mailto, and manual unsubscribe candidates without exposing URLs.
- Groups activity by account and exact sender domain.
- Supports Keep, Needs Review, Archive, Delete, and Unsubscribe decisions.
- Moves deleted mail to Trash without permanent expunge.
- Runs locally over stdio or remotely over authenticated Streamable HTTP.
- Supports one account with simple variables or multiple explicitly scoped accounts.

## How cleanup works

1. Connect with a Yahoo app password.
2. Scan message headers into SQLite.
3. Review messages or sender groups.
4. Tag an account/domain pair with a decision.
5. Preview the exact affected snapshot.
6. Approve `execute_decisions` to Archive, move to Trash, or unsubscribe.

Nothing modifies Yahoo Mail until `execute_decisions` runs against an explicitly
tagged account and sender domain.

## Five-minute local setup

Requirements:

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A Yahoo app password
- An MCP client such as Cursor or Claude Desktop

Generate an app password from
[Yahoo Account Security](https://login.yahoo.com/myaccount/security). Never use
your regular Yahoo password.

```bash
git clone https://github.com/ktrann24/yahoo-mail-mcp.git
cd yahoo-mail-mcp
uv sync
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```env
YAHOO_ACCOUNT_NAME=personal
YAHOO_EMAIL=you@yahoo.com
YAHOO_APP_PASSWORD=your-yahoo-app-password
```

Register it with Cursor:

```bash
cp .cursor/mcp.json.example .cursor/mcp.json
```

Replace the placeholder in `.cursor/mcp.json` with the absolute path to your
clone, then restart Cursor. The real file is ignored because it is
machine-specific.

Start by asking:

> List my configured accounts, then scan up to 100 messages from the Inbox of
> account `personal`. Do not modify any mail.

## Connect a Notion Custom Agent

Notion needs a publicly reachable Streamable HTTP endpoint. The supported
deployment uses Railway with a persistent volume, HTTPS, Host validation, and
bearer authentication.

Follow the complete [Railway and Notion setup guide](docs/railway.md).

After connecting, keep Notion's write-tool policy set to **Always ask** for:

- `set_decisions`, which only records account-scoped review choices.
- `execute_decisions`, which performs Archive, Delete, or Unsubscribe actions.

## Multiple accounts

Use `YAHOO_ACCOUNTS` instead of the three single-account variables:

```env
YAHOO_ACCOUNTS='[
  {"name":"personal","email":"one@yahoo.com","app_password":"app-password-one"},
  {"name":"work","email":"two@yahoo.com","app_password":"app-password-two"}
]'
```

Names must be unique. Sender groups, decisions, previews, and execution are
scoped by account. Mutating tools require an explicit account when more than
one is configured.

## Example MCP prompts

Read-only exploration:

> List configured accounts and show the scan status. Do not modify mail.

> Start a background scan of at most 100 Inbox messages for account `personal`,
> then report the job ID.

> Show the largest sender groups for account `personal`, including counts,
> recent dates, and available unsubscribe methods.

> Show one-click unsubscribe candidates for account `personal`, sorted by
> message count. Do not unsubscribe yet.

Safe review:

> Tag `newsletter.example.com` as Archive for account `personal`. Do not execute
> the decision.

> Preview Archive decisions for account `personal` and show the exact count and
> sender breakdown. Do not execute.

## Tools

Read and scan:

- `list_accounts`
- `scan_mailbox`, `start_scan_job`, `get_scan_job`, `list_scan_jobs`
- `get_scan_status`, `triage_new_mail`
- `list_recent_messages`, `search_messages`, `get_message_headers`
- `list_sender_groups`, `get_sender_detail`
- `list_unsubscribe_candidates`

Review and mutation:

- `set_decisions`: records an account/domain decision without touching mail.
- `preview_cleanup`: returns exact counts and a short-lived confirmation token.
- `execute_decisions`: performs approved Archive, Delete, or Unsubscribe actions.
- `export_review_csv`, `import_review_csv`: local-only spreadsheet round trip.

Remote mode intentionally omits CSV tools because arbitrary server filesystem
paths are unsafe over the network.

## Safety and privacy

- Scans use read-only folders and `BODY.PEEK` for selected header fields.
- UIDVALIDITY changes invalidate stale folder data.
- Decisions and mutations are scoped to an explicit account and domain.
- Archive and Delete use recoverable `UID MOVE` operations.
- Large moves require a 15-minute, single-use token bound to the exact snapshot.
- Live UIDVALIDITY and sender domains are checked before moves.
- One-click unsubscribe blocks private addresses, unrelated domains, DNS
  rebinding, and redirects.
- Candidate listings identify advertised methods; execution repeats final
  endpoint and DNS safety validation.
- Destructive actions are written to the SQLite audit log.
- Browse tools do not expose raw unsubscribe URLs.

SQLite stores account aliases, folders, UIDs, sender metadata, subjects, dates,
sizes, unsubscribe headers, decisions, checkpoints, and audit logs. It does not
store message bodies or attachments. Protect the database and hosting
environment as sensitive personal data.

See [SECURITY.md](SECURITY.md) for the complete deployment assumptions and
private vulnerability reporting process.

## Configuration

Common variables:

- `YAHOO_EMAIL`, `YAHOO_APP_PASSWORD`, `YAHOO_ACCOUNT_NAME`: one account.
- `YAHOO_ACCOUNTS`: JSON array for multiple accounts.
- `YAHOO_MAIL_MCP_DB`: SQLite path.
- `YAHOO_MAIL_MCP_DELETE_THRESHOLD`: large-move confirmation threshold.
- `YAHOO_MAIL_MCP_BATCH_SIZE`: IMAP fetch batch size.
- `YAHOO_MAIL_MCP_LOG_LEVEL`: Python log level.

Hosted mode also requires a bearer token and allowed hostname. See
[`.env.example`](.env.example) and [docs/railway.md](docs/railway.md).

## Why Yahoo needs special handling

Large Yahoo mailboxes may expose only a limited standard IMAP window. This
project handles Yahoo's `UIDONLY`, `PARTIAL`, and `MESSAGELIMIT` extensions
internally, using checkpointed batches and constant memory instead of requiring
thousands of MCP tool calls.

## Alpha limitations

- This is self-hosted software for one trusted owner, not a multi-tenant service.
- Yahoo app passwords and indexed metadata live wherever it is deployed.
- Very large historical scans may require multiple background jobs.
- Sender-provided unsubscribe headers can be missing or require manual action.
- Live mailbox mutations remain manual release checks.

See the [public release checklist](docs/RELEASE_CHECKLIST.md).

## Development

Use synthetic fixtures only—never commit real mailbox data or credentials.

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy src/yahoo_mail_mcp
uv run pytest -q
uv build
uv run twine check dist/*
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
