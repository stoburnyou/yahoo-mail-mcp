"""Review tools: sender groups, decisions, CSV round-trip."""

from __future__ import annotations

import csv
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..analysis import grouping
from ..app import AppContext
from ..store.db import VALID_DECISIONS
from .account_scope import resolve_account
from .annotations import READ_ONLY_LOCAL, WRITE_FILESYSTEM, WRITE_LOCAL

CSV_COLUMNS = [
    "account",
    "sender_domain",
    "decision",
    "message_count",
    "total_size_mb",
    "first_seen",
    "last_seen",
    "unsubscribe_available",
    "unsubscribe_methods",
    "accounts",
    "sender_addresses",
    "sample_subjects",
    "notes",
]


def register(mcp: FastMCP, ctx: AppContext, *, include_file_tools: bool = True) -> None:
    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def list_sender_groups(
        account: str | None = None,
        sort: str = "count",
        min_count: int = 1,
        decision: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List scanned mail grouped by account and sender domain.

        Every group includes its account. Pass `account` to limit the result.
        `sort` is one of: count, size, recent, oldest, domain.
        `decision` filters by tag: keep, unsubscribe, archive, delete, needs_review
        (needs_review includes untagged domains). Paginate with limit/offset.
        """
        acct_name = ctx.account(account).name if account else None
        groups = grouping.list_sender_groups(
            ctx.store.conn,
            account=acct_name,
            sort=sort,
            min_count=min_count,
            decision=decision,
            limit=limit,
            offset=offset,
        )
        return {"count": len(groups), "offset": offset, "groups": groups}

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def get_sender_detail(domain: str, account: str | None = None) -> dict:
        """Full detail for one sender domain: addresses, folder breakdown,
        up to 20 sample subjects, and unsubscribe methods for one account."""
        account_name = resolve_account(ctx, account)
        detail = grouping.get_sender_detail(ctx.store.conn, account_name, domain)
        if detail is None:
            return {
                "error": (f"No scanned messages for domain {domain!r} in account {account_name!r}")
            }
        return detail

    @mcp.tool(annotations=WRITE_LOCAL)
    def set_decisions(entries: list[dict], account: str | None = None) -> dict:
        """Batch-tag sender domains with account-scoped cleanup decisions.

        Each entry: {"account": "personal", "domain": "example.com",
        "decision": "keep|unsubscribe|archive|delete|needs_review",
        "notes": "optional"}. `account` may be supplied once as a tool argument
        or per entry. It is inferred only when exactly one account is configured.
        This tool never modifies Yahoo Mail.
        """
        applied = []
        errors = []
        for entry in entries:
            domain = (entry.get("domain") or "").strip().lower()
            decision = (entry.get("decision") or "").strip().lower()
            if not domain or decision not in VALID_DECISIONS:
                errors.append(
                    f"Invalid entry: {entry!r} (decision must be one of {VALID_DECISIONS})"
                )
                continue
            try:
                account_name = resolve_account(ctx, entry.get("account") or account)
            except (KeyError, ValueError) as exc:
                errors.append(f"Invalid entry for {domain!r}: {exc}")
                continue
            ctx.store.set_decision(
                account_name,
                domain,
                decision,
                notes=entry.get("notes"),
            )
            applied.append({"account": account_name, "domain": domain, "decision": decision})
        ctx.store.log_action("set_decisions", count=len(applied), detail=str(applied[:20]))
        return {"applied": applied, "errors": errors}

    if not include_file_tools:
        return

    @mcp.tool(annotations=WRITE_FILESYSTEM)
    def export_review_csv(path: str) -> dict:
        """Export all sender groups to a CSV for spreadsheet review.

        Edit the `decision` column (keep / unsubscribe / archive / delete / needs_review)
        and re-import with import_review_csv.
        """
        groups = grouping.list_sender_groups(ctx.store.conn, limit=1_000_000)
        decisions_notes = {
            (row["account"], row["sender_domain"]): row["notes"]
            for row in ctx.store.get_decisions()
        }
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for g in groups:
                writer.writerow(
                    {
                        "account": g["account"],
                        "sender_domain": g["sender_domain"],
                        "decision": g["decision"],
                        "message_count": g["message_count"],
                        "total_size_mb": g["total_size_mb"],
                        "first_seen": g["first_seen"],
                        "last_seen": g["last_seen"],
                        "unsubscribe_available": g["unsubscribe"]["available"],
                        "unsubscribe_methods": ";".join(g["unsubscribe"]["methods"]),
                        "accounts": ";".join(g["accounts"]),
                        "sender_addresses": ";".join(g["sender_addresses"]),
                        "sample_subjects": " | ".join(
                            s.replace("|", "/") for s in g["sample_subjects"]
                        ),
                        "notes": decisions_notes.get((g["account"], g["sender_domain"])) or "",
                    }
                )
        return {"path": str(out), "rows": len(groups)}

    @mcp.tool(annotations=WRITE_FILESYSTEM)
    def import_review_csv(path: str) -> dict:
        """Import decisions from a CSV previously produced by export_review_csv.

        Only account, sender_domain, decision and notes are read. Account may
        be omitted only when exactly one account is configured.
        """
        src = Path(path).expanduser()
        if not src.exists():
            return {"error": f"File not found: {src}"}
        applied = 0
        errors = []
        with src.open(newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                domain = (row.get("sender_domain") or "").strip().lower()
                decision = (row.get("decision") or "").strip().lower()
                if not domain or not decision:
                    continue
                if decision not in VALID_DECISIONS:
                    errors.append(f"Row {i}: invalid decision {decision!r}")
                    continue
                try:
                    account_name = resolve_account(ctx, row.get("account") or None)
                except (KeyError, ValueError) as exc:
                    errors.append(f"Row {i}: {exc}")
                    continue
                ctx.store.set_decision(
                    account_name,
                    domain,
                    decision,
                    notes=(row.get("notes") or None),
                    source="csv",
                )
                applied += 1
        ctx.store.log_action("import_review_csv", count=applied, detail=str(src))
        return {"applied": applied, "errors": errors}
