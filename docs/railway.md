# Railway and Notion setup

Use Railway when a Notion Custom Agent needs continuous access to the MCP
server. The deployment keeps SQLite on a persistent volume and runs background
scans in a long-lived container.

Vercel is not recommended because its local filesystem is ephemeral and it
cannot run a durable background worker.

## 1. Deploy the repository

1. Fork or push the repository to your GitHub account.
2. Create a Railway project from that repository.
3. Add a persistent Railway volume mounted at `/data`.
4. Generate a Railway public domain.
5. Copy the hostname, such as `your-service.up.railway.app`, without `https://`
   or a trailing path.

Railway builds the included `Dockerfile`, starts `yahoo-mail-mcp-http`, and
checks `GET /health`.

## 2. Generate the bearer token

Generate a 32-byte token locally:

```bash
openssl rand -hex 32
```

Store it securely. The same raw value must be configured in Railway and
Notion. Do not commit it or paste it into an issue or chat.

## 3. Configure one Yahoo account

Add these Railway service variables:

```env
YAHOO_ACCOUNT_NAME=personal
YAHOO_EMAIL=you@yahoo.com
YAHOO_APP_PASSWORD=your-yahoo-app-password
YAHOO_MAIL_MCP_DB=/data/mail.db
YAHOO_MAIL_MCP_BEARER_TOKEN=your-generated-token
YAHOO_MAIL_MCP_ALLOWED_HOSTS=your-service.up.railway.app
YAHOO_MAIL_MCP_REQUIRE_HTTPS=true
```

`YAHOO_MAIL_MCP_ALLOWED_HOSTS` must contain only the exact hostname. Do not
include `https://` or `/mcp`.

After saving variables, wait for the new deployment to become active.

## 4. Configure multiple Yahoo accounts

Replace `YAHOO_ACCOUNT_NAME`, `YAHOO_EMAIL`, and `YAHOO_APP_PASSWORD` with:

```env
YAHOO_ACCOUNTS=[
  {"name":"personal","email":"one@yahoo.com","app_password":"app-password-one"},
  {"name":"work","email":"two@yahoo.com","app_password":"app-password-two"}
]
```

Do not configure both formats. Account names must be unique. The server requires
an explicit account for multi-account decisions, previews, and execution.

## 5. Verify Railway

Open:

```text
https://your-service.up.railway.app/health
```

Expected response:

```json
{"status":"ok","transport":"streamable-http"}
```

This health route is public and contains no account data. All requests to
`/mcp` require HTTPS, an allowed Host header, and the bearer token.

## 6. Optional private Yahoo SMTP relay

If the MCP service can read Yahoo Mail but cannot open outbound SMTP
connections to Yahoo, deploy a second private service from the same image with
this start command:

```text
yahoo-smtp-relay
```

Give the relay its own public HTTPS domain and configure only these variables
on the relay service:

```env
YAHOO_SMTP_RELAY_SECRET=relay-shared-secret-at-least-32-characters
YAHOO_SMTP_RELAY_SMTP_HOST=smtp.mail.yahoo.com
YAHOO_SMTP_RELAY_SMTP_PORT=587
YAHOO_SMTP_RELAY_ATTACHMENT_HOST_SUFFIXES=.dropboxusercontent.com,.dropbox.com
```

The relay exposes only `GET /health` and `POST /send`. It does not expose any
mailbox delete, move, archive, trash, or expunge operation. `POST /send`
requires `Authorization: Bearer <YAHOO_SMTP_RELAY_SECRET>`.

Then configure the MCP service with a separate client-side copy of the relay
secret:

```env
YAHOO_MAIL_MCP_RELAY_URL=https://your-relay-service.up.railway.app
YAHOO_MAIL_MCP_RELAY_SECRET=relay-shared-secret-at-least-32-characters
```

Keep `YAHOO_MAIL_MCP_RELAY_SECRET` distinct from
`YAHOO_MAIL_MCP_BEARER_TOKEN`. The normal mail flow remains:

```text
preview_send_email -> explicit user approval -> send_email -> relay HTTPS -> Yahoo SMTP
```

The relay validates recipient lists, subject, body, attachment sizes, and
attachment URL host allowlists before SMTP send. Audit logs avoid message body,
subject text, full recipient addresses, attachment URLs, and Yahoo app
passwords.

## 7. Connect Notion

1. Ask a workspace administrator to enable custom MCP servers under
   **Settings → Notion AI → AI connectors**.
2. Open the Custom Agent's **Settings → Tools & Access**.
3. Choose **Add connection → Custom MCP server**.
4. Enter:

   ```text
   https://your-service.up.railway.app/mcp
   ```

5. Choose Bearer token authentication.
6. Paste the raw token from Railway. Do not add the word `Bearer` or quotes.
7. Save the connection.
8. Keep the write-tool policy set to **Always ask**.

Verify the account before scanning:

> Call `list_accounts` and show the configured account name and email address.
> Do not modify any mail.

Then run a bounded first scan:

> Start a background scan of at most 100 Inbox messages for account `personal`.
> Do not modify any mail.

After the scan completes:

> Show one-click unsubscribe candidates for account `personal`, sorted by
> message count. Do not unsubscribe yet.

## Troubleshooting

### Host not allowed

Response:

```text
400 {"error":"Host not allowed"}
```

Set `YAHOO_MAIL_MCP_ALLOWED_HOSTS` to the exact Railway hostname and redeploy.

### Authentication failed

Response:

```text
401 {"error":"Unauthorized"}
```

The values in Notion and `YAHOO_MAIL_MCP_BEARER_TOKEN` do not match. Generate a
new token, paste the same raw value into both places, redeploy Railway, and
recreate the Notion connection if it cached old credentials.

### Health works but Notion cannot connect

Confirm that:

- Notion uses `/mcp`, not `/health`.
- The MCP URL begins with `https://`.
- The Railway deployment became active after the latest variable change.
- The allowed-host variable contains no scheme or path.
- Notion's bearer field contains only the token.

### Background scan was interrupted

Call `list_scan_jobs`. A server restart marks active jobs as interrupted, but
scan checkpoints remain durable. Start another scan job for the same account to
resume.

## Operational guidance

- Protect the Railway account with MFA.
- Restrict project access.
- Keep the `/data` volume attached across deployments.
- Rotate Yahoo app passwords and the bearer token after suspected exposure.
- Never expose `/mcp` without authentication.
- Test Archive, Delete, and Unsubscribe on a small, carefully selected sample.
