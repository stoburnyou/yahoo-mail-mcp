# Security Policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature or open a private
security advisory for this repository. Do not include credentials, app
passwords, mailbox exports, message contents, or unsubscribe tokens in a
public issue.

Reports are especially helpful for:

- exposure of Yahoo credentials or locally indexed mail metadata;
- IMAP UID or UIDVALIDITY handling that could target the wrong message;
- account-scoping failures that could affect another configured mailbox;
- bypasses of cleanup previews or confirmation tokens;
- server-side request forgery in one-click unsubscribe handling;
- unexpected mailbox mutation from a read-only tool.

Include a minimal synthetic reproduction, affected version or commit, expected
behavior, and observed behavior. You should receive an acknowledgement within
seven days.

## Deployment assumptions

Local stdio mode has no network listener. Hosted mode exposes Streamable HTTP
and requires a high-entropy bearer token, HTTPS, an allowed Host header, and
persistent private storage. Do not disable these controls or place a second
unauthenticated proxy route in front of `/mcp`.

Remote deployment is designed for a single trusted owner, not as a multi-tenant
mail service. MCP tool annotations help clients display confirmation UI but are
advisory and are not a substitute for server authentication or cleanup tokens.

Treat hosting variables, `.env`, the SQLite database and volume, CSV exports,
MCP logs, and any future message-content output as sensitive personal data.
Rotate the HTTP bearer token and Yahoo app passwords after suspected exposure.
