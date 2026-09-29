from types import SimpleNamespace

import pytest

from yahoo_mail_mcp.config import Account
from yahoo_mail_mcp.imap.client import FolderInfo
from yahoo_mail_mcp.server import build_server


class FakeImap:
    def __init__(self, folders):
        self.folders = folders
        self.created = []
        self.renamed = []
        self.deleted = []

    def list_folders(self):
        return self.folders

    def create_folder(self, folder):
        self.created.append(folder)
        self.folders.append(FolderInfo(folder, None, 0, None, None))

    def rename_folder(self, old_name, new_name):
        self.renamed.append((old_name, new_name))
        for folder in self.folders:
            if folder.name == old_name:
                folder.name = new_name

    def delete_folder(self, folder):
        self.deleted.append(folder)
        self.folders = [existing for existing in self.folders if existing.name != folder]


class FakeContext:
    def __init__(self, store, imap):
        self.store = store
        self._imap = imap
        self.settings = SimpleNamespace(
            accounts=[Account("personal", "me@example.com", "secret")],
            delete_threshold=100,
            batch_size=500,
        )

    def account(self, account):
        return self.settings.accounts[0]

    def imap(self, account):
        return self._imap


def _tool(server, name):
    tools = {tool.name: tool.fn for tool in server._tool_manager._tools.values()}  # noqa: SLF001
    return tools[name]


@pytest.fixture(autouse=True)
def token_secret(monkeypatch):
    monkeypatch.setenv("YAHOO_MAIL_MCP_WRITE_TOKEN_SECRET", "x" * 32)


def test_create_folder_previews_then_executes(store):
    imap = FakeImap([FolderInfo("Inbox", None, 1, None, None)])
    server = build_server(FakeContext(store, imap), remote=True)
    create_folder = _tool(server, "create_folder")

    preview = create_folder("personal", "02 FINANCE")
    assert preview["ok"] is True
    assert imap.created == []

    result = create_folder("personal", "02 FINANCE", preview["confirm_token"])

    assert result["ok"] is True
    assert imap.created == ["02 FINANCE"]


def test_rename_folder_blocks_system_folders(store):
    imap = FakeImap([FolderInfo("Sent", "\\Sent", 1, None, None)])
    server = build_server(FakeContext(store, imap), remote=True)
    rename_folder = _tool(server, "rename_folder")

    result = rename_folder("personal", "Sent", "Old Sent")

    assert result == {"ok": False, "error": "Refusing to rename a system folder"}
    assert imap.renamed == []


def test_delete_empty_folder_requires_zero_messages(store):
    imap = FakeImap([FolderInfo("Projects", None, 3, None, None)])
    server = build_server(FakeContext(store, imap), remote=True)
    delete_empty_folder = _tool(server, "delete_empty_folder")

    result = delete_empty_folder("personal", "Projects")

    assert result == {"ok": False, "error": "Folder is not empty", "messages": 3}
    assert imap.deleted == []


def test_delete_empty_folder_previews_then_executes(store):
    imap = FakeImap([FolderInfo("Empty", None, 0, None, None)])
    server = build_server(FakeContext(store, imap), remote=True)
    delete_empty_folder = _tool(server, "delete_empty_folder")

    preview = delete_empty_folder("personal", "Empty")
    assert preview["ok"] is True
    assert imap.deleted == []

    result = delete_empty_folder("personal", "Empty", preview["confirm_token"])

    assert result["ok"] is True
    assert imap.deleted == ["Empty"]
