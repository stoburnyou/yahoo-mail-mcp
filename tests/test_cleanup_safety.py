from conftest import make_message

from yahoo_mail_mcp.imap.actions import move_to_archive, move_to_trash
from yahoo_mail_mcp.tools import execute


class RetryImap:
    def with_retry(self, _name, fn):
        return fn()


def test_spot_check_selects_folder_and_fails_closed(monkeypatch):
    class Imap(RetryImap):
        def __init__(self):
            self.selected = None

        def select_folder(self, folder, readonly=True):
            self.selected = (folder, readonly)
            return {b"UIDVALIDITY": 10}

    imap = Imap()
    monkeypatch.setattr(execute, "_raw_uid_fetch", lambda *_args, **_kwargs: [])

    error = execute._spot_check(
        imap,
        "personal",
        "Inbox",
        [{"uid": 1, "sender_domain": "example.com"}],
        expected_uidvalidity=10,
    )

    assert imap.selected == ("Inbox", True)
    assert error and "missing or unreadable" in error


def test_move_counts_only_confirmed_source_uids(store):
    store.upsert_messages([make_message(uid=1), make_message(uid=2)])

    class LowLevel:
        def __init__(self, owner):
            self.owner = owner

        def uid(self, command, _uids, _trash):
            assert command == "MOVE"
            self.owner.moved = True
            return "OK", [b"done"]

    class Client:
        def __init__(self):
            self.moved = False
            self._imap = LowLevel(self)

        def search(self, _criteria):
            return [] if self.moved else [1, 2]

    class Imap(RetryImap):
        def __init__(self):
            self.client = Client()

        def find_special_folder(self, _special_use):
            return "Trash"

        def select_folder(self, _folder, readonly=False):
            return {b"UIDVALIDITY": 100}

    moved = move_to_trash(Imap(), store, "personal", "Inbox", [1, 2, 3], expected_uidvalidity=100)

    assert moved == 2
    assert store.message_count("personal") == 0


def test_archive_uses_archive_special_folder(store):
    store.upsert_messages([make_message(uid=1)])

    class LowLevel:
        def __init__(self, owner):
            self.owner = owner

        def uid(self, command, _uids, destination):
            assert command == "MOVE"
            assert destination == '"Archive"'
            self.owner.moved = True
            return "OK", [b"done"]

    class Client:
        def __init__(self):
            self.moved = False
            self._imap = LowLevel(self)

        def search(self, _criteria):
            return [] if self.moved else [1]

    class Imap(RetryImap):
        def __init__(self):
            self.client = Client()

        def find_special_folder(self, special_use):
            assert special_use == "\\Archive"
            return "Archive"

        def select_folder(self, _folder, readonly=False):
            return {b"UIDVALIDITY": 100}

    moved = move_to_archive(
        Imap(),
        store,
        "personal",
        "Inbox",
        [1],
        expected_uidvalidity=100,
    )

    assert moved == 1
    assert store.message_count("personal") == 0
