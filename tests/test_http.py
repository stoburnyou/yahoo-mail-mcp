import asyncio
from types import SimpleNamespace

from starlette.testclient import TestClient

from yahoo_mail_mcp.http import create_http_app
from yahoo_mail_mcp.server import build_server


class FakeContext:
    def __init__(self, store):
        self.store = store
        self.settings = SimpleNamespace(accounts=[], delete_threshold=100, batch_size=500)
        self.closed = False

    def close(self):
        self.closed = True


def test_http_health_and_bearer_auth(store):
    ctx = FakeContext(store)
    app = create_http_app(
        ctx=ctx,
        bearer_token="a" * 32,
        allowed_hosts={"testserver"},
        require_https=False,
    )

    with TestClient(app) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "transport": "streamable-http",
        }
        response = client.post("/mcp", json={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

        response = client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Bearer {'a' * 32}"},
        )
        assert response.status_code != 401

    assert ctx.closed is True


def test_http_rejects_unlisted_host(store):
    app = create_http_app(
        ctx=FakeContext(store),
        bearer_token="b" * 32,
        allowed_hosts={"mail.example.com"},
        require_https=False,
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Bearer {'b' * 32}"},
        )
    assert response.status_code == 400
    assert response.json() == {"error": "Host not allowed"}


def test_http_requires_strong_token(store):
    try:
        create_http_app(ctx=FakeContext(store), bearer_token="short")
    except ValueError as exc:
        assert "at least 32 characters" in str(exc)
    else:
        raise AssertionError("short bearer token was accepted")


def test_remote_tools_exclude_file_access_and_publish_safety_annotations(store):
    remote = build_server(FakeContext(store), remote=True)
    tools = {tool.name: tool for tool in asyncio.run(remote.list_tools())}

    assert "export_review_csv" not in tools
    assert "import_review_csv" not in tools
    assert tools["list_recent_messages"].annotations.readOnlyHint is True
    assert tools["set_decisions"].annotations.destructiveHint is False
    assert tools["execute_decisions"].annotations.destructiveHint is True


def test_local_tools_keep_csv_round_trip(store):
    local = build_server(FakeContext(store))
    names = {tool.name for tool in asyncio.run(local.list_tools())}

    assert "export_review_csv" in names
    assert "import_review_csv" in names
