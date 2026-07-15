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
- bypasses of cleanup previews or confirmation tokens;
- server-side request forgery in one-click unsubscribe handling;
- unexpected mailbox mutation from a read-only tool.

Include a minimal synthetic reproduction, affected version or commit, expected
behavior, and observed behavior. You should receive an acknowledgement within
seven days.

## Deployment assumptions

This project is designed as a local stdio MCP server. Do not expose it as an
unauthenticated network service. Treat `.env`, the SQLite database, CSV
exports, MCP logs, and any future message-content output as sensitive personal
data.
