# Changelog

All notable changes are documented here. This project follows semantic
versioning once stable releases begin.

## Unreleased

- Scope cleanup decisions and mutations to an explicit Yahoo account.
- Invalidate legacy global decisions during migration so they cannot
  accidentally apply to another configured account.
- Add simple single-account environment variables alongside multi-account JSON.
- Add authenticated Streamable HTTP deployment for Railway and Notion.
- Add durable background scan jobs and recoverable Archive actions.
- Publish MCP safety annotations for read, write, and destructive tools.
- Add a privacy-safe, account-scoped tool for listing one-click, mailto, and
  manual unsubscribe candidates.

## 0.1.0 - 2026-07-14

- Initial alpha implementation for resumable Yahoo header scans, sender-domain
  review, cleanup previews, recoverable deletes, and unsubscribe handling.
