"""Review tools: sender groups, decisions, CSV round-trip."""

from __future__ import annotations

import csv
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..analysis import grouping
from ..app import AppContext
from ..store.db import VALID_DECISIONS

CSV_COLUMNS = [
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


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def list_sender_groups(
        account: str | None = None,
        sort: str = "count",
        min_count: int = 1,
        decision: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List scanned mail grouped by sender domain.

        `sort` is one of: count, size, recent, oldest, domain.
        `decision` filters by tag: keep, unsubscribe, delete, needs_review
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

    @mcp.tool()
    def get_sender_detail(domain: str) -> dict:
        """Full detail for one sender domain: addresses, folder breakdown,
        up to 20 sample subjects, and the unsubscribe methods detected."""
        detail = grouping.get_sender_detail(ctx.store.conn, domain)
        if detail is None:
            return {"error": f"No scanned messages for domain {domain!r}"}
        return detail

    @mcp.tool()
    def set_decisions(entries: list[dict]) -> dict:
        """Batch-tag sender domains with cleanup decisions.

        Each entry: {"domain": "example.com", "decision": "keep|unsubscribe|delete|needs_review",
        "notes": "optional"}. Decisions only take effect when execute_decisions
        is called later; nothing is deleted or unsubscribed by this tool.
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
            ctx.store.set_decision(domain, decision, notes=entry.get("notes"))
            applied.append({"domain": domain, "decision": decision})
        ctx.store.log_action("set_decisions", count=len(applied), detail=str(applied[:20]))
        return {"applied": applied, "errors": errors}

    @mcp.tool()
    def export_review_csv(path: str) -> dict:
        """Export all sender groups to a CSV for spreadsheet review.

        Edit the `decision` column (keep / unsubscribe / delete / needs_review)
        and re-import with import_review_csv.
        """
        groups = grouping.list_sender_groups(ctx.store.conn, limit=1_000_000)
        decisions_notes = {row["sender_domain"]: row["notes"] for row in ctx.store.get_decisions()}
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for g in groups:
                writer.writerow(
                    {
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
                        "notes": decisions_notes.get(g["sender_domain"]) or "",
                    }
                )
        return {"path": str(out), "rows": len(groups)}

    @mcp.tool()
    def import_review_csv(path: str) -> dict:
        """Import decisions from a CSV previously produced by export_review_csv.

        Only the sender_domain, decision and notes columns are read. Rows with
        an empty or unchanged-from-default decision are skipped.
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
                ctx.store.set_decision(
                    domain, decision, notes=(row.get("notes") or None), source="csv"
                )
                applied += 1
        ctx.store.log_action("import_review_csv", count=applied, detail=str(src))
        return {"applied": applied, "errors": errors}
