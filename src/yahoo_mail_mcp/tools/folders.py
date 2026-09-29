"""Remote-safe Yahoo folder management with explicit preview/confirm tokens."""

from __future__ import annotations

import hashlib
import json
import re

from mcp.server.fastmcp import FastMCP

from ..app import AppContext
from ..imap.client import FolderInfo
from .annotations import DESTRUCTIVE_REMOTE
from .mail_actions import TOKEN_TTL, _check_token, _make_token

MAX_FOLDER_NAME_LENGTH = 120
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _folder_payload(account: str, action: str, **values: str | int) -> dict:
    return {"account": account, "action": action, **values}


def _hash_name(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _validate_folder_name(name: str, *, field: str = "folder_name") -> list[str]:
    errors: list[str] = []
    if name != name.strip():
        errors.append(f"{field} must not start or end with whitespace")
    if not name.strip():
        errors.append(f"{field} is required")
    if len(name) > MAX_FOLDER_NAME_LENGTH:
        errors.append(f"{field} must be at most {MAX_FOLDER_NAME_LENGTH} characters")
    if CONTROL_CHARS.search(name):
        errors.append(f"{field} must not contain control characters")
    if any(ch in name for ch in ('"', "\\", "/", "\r", "\n")):
        errors.append(f"{field} must not contain quotes, slashes, or newlines")
    return errors


def _find_folder(folders: list[FolderInfo], name: str) -> FolderInfo | None:
    return next((folder for folder in folders if folder.name == name), None)


def _is_system_folder(info: FolderInfo | None, name: str) -> bool:
    if info is not None and info.special_use:
        return True
    return name.strip().lower() in {
        "inbox",
        "sent",
        "sent messages",
        "draft",
        "drafts",
        "archive",
        "archives",
        "all",
        "all mail",
        "spam",
        "bulk",
        "bulk mail",
        "junk",
        "trash",
        "deleted",
        "deleted items",
    }


def _safe_detail(action: str, **values: str | int) -> str:
    safe = {"action": action, **values}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"))


def _imap_create_folder(imap, folder_name: str) -> None:
    if hasattr(imap, "create_folder"):
        imap.create_folder(folder_name)
    else:
        imap.with_retry(f"create folder {folder_name}", lambda: imap.client.create_folder(folder_name))


def _imap_rename_folder(imap, old_folder_name: str, new_folder_name: str) -> None:
    if hasattr(imap, "rename_folder"):
        imap.rename_folder(old_folder_name, new_folder_name)
    else:
        imap.with_retry(
            f"rename folder {old_folder_name}",
            lambda: imap.client.rename_folder(old_folder_name, new_folder_name),
        )


def _imap_delete_folder(imap, folder_name: str) -> None:
    if hasattr(imap, "delete_folder"):
        imap.delete_folder(folder_name)
    else:
        imap.with_retry(f"delete folder {folder_name}", lambda: imap.client.delete_folder(folder_name))


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def create_folder(account: str, folder_name: str, confirm_token: str | None = None) -> dict:
        """Preview or create one custom Yahoo folder. Pass confirm_token to execute."""
        acct = ctx.account(account)
        folder_name = folder_name or ""
        errors = _validate_folder_name(folder_name)
        if errors:
            return {"ok": False, "errors": errors}

        imap = ctx.imap(acct.name)
        folders = imap.list_folders()
        existing = _find_folder(folders, folder_name)
        if existing is not None:
            return {"ok": False, "error": "Folder already exists"}
        if _is_system_folder(None, folder_name):
            return {"ok": False, "error": "Refusing to create a reserved system folder name"}

        payload = _folder_payload(acct.name, "create_folder", folder_name=folder_name)
        if not confirm_token:
            return {
                "ok": True,
                "preview": {"account": acct.name, "action": "create_folder", "folder_name": folder_name},
                "confirm_token": _make_token("folder_action", payload),
                "confirm_token_expires_in_seconds": TOKEN_TTL,
                "note": "Nothing has been changed yet. Re-run create_folder with this confirm_token after explicit approval.",
            }

        err = _check_token(confirm_token, "folder_action", payload)
        if err:
            return {"ok": False, "error": err}
        _imap_create_folder(imap, folder_name)
        ctx.store.log_action(
            "create_folder",
            account=acct.name,
            folder=folder_name,
            count=1,
            detail=_safe_detail("create_folder", folder_sha256=_hash_name(folder_name)),
        )
        return {"ok": True, "action": "create_folder", "created": folder_name}

    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def rename_folder(
        account: str,
        old_folder_name: str,
        new_folder_name: str,
        confirm_token: str | None = None,
    ) -> dict:
        """Preview or rename one custom Yahoo folder. System folders are blocked."""
        acct = ctx.account(account)
        old_folder_name = old_folder_name or ""
        new_folder_name = new_folder_name or ""
        errors = _validate_folder_name(old_folder_name, field="old_folder_name")
        errors.extend(_validate_folder_name(new_folder_name, field="new_folder_name"))
        if old_folder_name == new_folder_name:
            errors.append("new_folder_name must be different")
        if errors:
            return {"ok": False, "errors": errors}

        imap = ctx.imap(acct.name)
        folders = imap.list_folders()
        old_info = _find_folder(folders, old_folder_name)
        if old_info is None:
            return {"ok": False, "error": "Source folder does not exist"}
        if _is_system_folder(old_info, old_folder_name):
            return {"ok": False, "error": "Refusing to rename a system folder"}
        if _find_folder(folders, new_folder_name) is not None:
            return {"ok": False, "error": "Destination folder already exists"}
        if _is_system_folder(None, new_folder_name):
            return {"ok": False, "error": "Refusing to rename to a reserved system folder name"}

        payload = _folder_payload(
            acct.name,
            "rename_folder",
            old_folder_name=old_folder_name,
            new_folder_name=new_folder_name,
        )
        if not confirm_token:
            return {
                "ok": True,
                "preview": {
                    "account": acct.name,
                    "action": "rename_folder",
                    "old_folder_name": old_folder_name,
                    "new_folder_name": new_folder_name,
                },
                "confirm_token": _make_token("folder_action", payload),
                "confirm_token_expires_in_seconds": TOKEN_TTL,
                "note": "Nothing has been changed yet. Re-run rename_folder with this confirm_token after explicit approval.",
            }

        err = _check_token(confirm_token, "folder_action", payload)
        if err:
            return {"ok": False, "error": err}
        _imap_rename_folder(imap, old_folder_name, new_folder_name)
        ctx.store.log_action(
            "rename_folder",
            account=acct.name,
            folder=new_folder_name,
            count=1,
            detail=_safe_detail(
                "rename_folder",
                old_folder_sha256=_hash_name(old_folder_name),
                new_folder_sha256=_hash_name(new_folder_name),
            ),
        )
        return {
            "ok": True,
            "action": "rename_folder",
            "old_folder_name": old_folder_name,
            "new_folder_name": new_folder_name,
        }

    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def delete_empty_folder(
        account: str,
        folder_name: str,
        confirm_token: str | None = None,
    ) -> dict:
        """Preview or delete one custom Yahoo folder only when it has exactly 0 messages."""
        acct = ctx.account(account)
        folder_name = folder_name or ""
        errors = _validate_folder_name(folder_name)
        if errors:
            return {"ok": False, "errors": errors}

        imap = ctx.imap(acct.name)
        folders = imap.list_folders()
        info = _find_folder(folders, folder_name)
        if info is None:
            return {"ok": False, "error": "Folder does not exist"}
        if _is_system_folder(info, folder_name):
            return {"ok": False, "error": "Refusing to delete a system folder"}
        if info.messages is None:
            return {"ok": False, "error": "Folder message count is unavailable; refusing to delete"}
        if int(info.messages) != 0:
            return {"ok": False, "error": "Folder is not empty", "messages": int(info.messages)}

        payload = _folder_payload(acct.name, "delete_empty_folder", folder_name=folder_name, messages=0)
        if not confirm_token:
            return {
                "ok": True,
                "preview": {
                    "account": acct.name,
                    "action": "delete_empty_folder",
                    "folder_name": folder_name,
                    "messages": 0,
                },
                "confirm_token": _make_token("folder_action", payload),
                "confirm_token_expires_in_seconds": TOKEN_TTL,
                "note": "Nothing has been changed yet. This never expunges or deletes messages.",
            }

        err = _check_token(confirm_token, "folder_action", payload)
        if err:
            return {"ok": False, "error": err}
        _imap_delete_folder(imap, folder_name)
        ctx.store.log_action(
            "delete_empty_folder",
            account=acct.name,
            folder=folder_name,
            count=1,
            detail=_safe_detail(
                "delete_empty_folder",
                folder_sha256=_hash_name(folder_name),
                messages=0,
            ),
        )
        return {
            "ok": True,
            "action": "delete_empty_folder",
            "deleted": folder_name,
            "messages": 0,
            "note": "No expunge or message deletion was performed.",
        }
