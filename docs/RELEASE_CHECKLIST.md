# Public release checklist

## Automated

- [ ] Formatting, lint, type checks, and tests pass.
- [ ] Python source distribution and wheel build successfully.
- [ ] Docker image builds in GitHub Actions.
- [ ] Working tree contains no database, environment, key, or mailbox export files.
- [ ] Current tree and Git history pass a secret scan.

## Manual, using a disposable or carefully selected mailbox sample

- [ ] Railway `/health` responds successfully.
- [ ] Notion connects to `/mcp` with bearer authentication.
- [ ] `list_accounts` reports the intended account.
- [ ] `start_scan_job` completes with `max_messages=100`.
- [ ] Sender groups and cached message headers match the selected account.
- [ ] A small Archive preview and execution target only the selected account.
- [ ] A small Delete preview moves mail to Trash without expunging.
- [ ] Unsubscribe is tested only against a mailing list safe to unsubscribe from.
- [ ] Recovery from Archive and Trash is verified.

Mailbox mutations require a human-selected account, sender domain, and decision.
Do not automate the manual mutation checks against a contributor's primary mail.
