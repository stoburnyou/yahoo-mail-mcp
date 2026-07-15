# yahoo-mail-mcp

MCP server for auditing and cleaning up Yahoo Mail accounts over IMAP. Built for
massive backlogs: it scans header-level data only (sender, subject, date, size,
List-Unsubscribe), groups everything by sender domain into a local SQLite
database, and executes unsubscribe/delete decisions only after you explicitly
tag them.

## Setup

1. Generate an app password for each Yahoo account at
   Yahoo Account Security -> "Generate app password".
2. Copy `.env.example` to `.env` and fill in `YAHOO_ACCOUNTS`.
3. Install:

```bash
uv sync
```

4. Register with your MCP client (e.g. Cursor / Claude Desktop):

```json
{
  "mcpServers": {
    "yahoo-mail": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/yahoo-mail-mcp", "yahoo-mail-mcp"]
    }
  }
}
```

## Tools

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Verify connectivity; list folders and counts per account |
| `scan_mailbox` | Bulk header scan into SQLite; checkpointed, resumable, incremental |
| `get_scan_status` | Scan progress without touching IMAP |
| `list_sender_groups` / `get_sender_detail` | Review mail grouped by sender domain |
| `set_decisions` | Tag domains: keep / unsubscribe / delete / needs_review |
| `export_review_csv` / `import_review_csv` | Spreadsheet round-trip for bulk review |
| `preview_cleanup` | Dry run with exact counts + confirm token |
| `execute_decisions` | The only destructive tool; moves mail to Trash / unsubscribes |
| `triage_new_mail` | Incremental scan of new mail with suggestions and flags |

## Safety model

- Scanning is read-only (`BODY.PEEK`, folders opened read-only).
- Deletes are `UID MOVE` to Trash - recoverable, never expunged.
- Bulk deletes over the threshold (default 1000, `YAHOO_MAIL_MCP_DELETE_THRESHOLD`)
  require a `confirm_token` from a fresh `preview_cleanup`.
- `UIDVALIDITY` is verified and live From headers are spot-checked before any move.
- Unsubscribes: RFC 8058 one-click POST or mailto only; plain links are
  reported for manual action, never auto-fetched.
- Every destructive action is recorded in the `action_log` table.

## Yahoo specifics handled

- App-password auth on `imap.mail.yahoo.com:993` / `smtp.mail.yahoo.com:465`.
- `ENABLE UIDONLY` + `PARTIAL` fetches to reach past the ~10k limited-mode
  window on large folders.
- Batches capped to the server-advertised `MESSAGELIMIT`.
- Reconnect with exponential backoff and checkpointed resume, since Yahoo
  drops connections under sustained load.
- One IMAP connection per account (Yahoo allows ~5 per IP total).

## Tests

```bash
uv run pytest
```
