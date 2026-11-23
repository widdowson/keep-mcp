import json

import pytest

from server import cli
from server.http_auth import (
    BearerAuthMiddleware,
    current_caller,
    load_token_map,
    new_token,
)


async def _echo_app(scope, receive, send):
    body = (current_caller.get() or "").encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture
def token_pair():
    return new_token()


async def _post(token_hashes, headers):
    """Drive the middleware with one ASGI POST; return (status, headers, body)."""
    app = BearerAuthMiddleware(_echo_app, token_hashes)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = sent[0]
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return start["status"], dict(start["headers"]), body.decode()


@pytest.mark.anyio
async def test_valid_token_sets_caller(token_pair):
    token, digest = token_pair
    status, _, body = await _post({digest: "andrew"}, {"Authorization": f"Bearer {token}"})
    assert status == 200
    assert body == "andrew"


@pytest.mark.anyio
@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", "Bearer "])
async def test_missing_or_wrong_token_is_rejected(token_pair, header):
    _, digest = token_pair
    headers = {"Authorization": header} if header else {}
    status, resp_headers, _ = await _post({digest: "andrew"}, headers)
    assert status == 401
    assert resp_headers[b"www-authenticate"].startswith(b"Bearer")


def test_load_token_map(tmp_path, token_pair):
    _, digest = token_pair
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"adrienne": digest.upper()}))
    assert load_token_map(path) == {digest: "adrienne"}


@pytest.mark.parametrize("content", [{}, {"a": "nothex"}, [], {"a": "0" * 64, "b": "0" * 64}])
def test_load_token_map_rejects_bad_files(tmp_path, content):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        load_token_map(path)


def test_main_uses_bearer_auth_with_token_map(monkeypatch, tmp_path, token_pair):
    _, digest = token_pair
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"andrew": digest}))
    captured = {}

    def fake_uvicorn_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(cli.mcp, "remove_tool", lambda name: None)
    monkeypatch.setattr(
        "sys.argv",
        ["keep-mcp", "--transport", "streamable-http", "--token-map", str(path),
         "--allowed-host", "keep-mcp.example.com"],
    )
    cli.main()
    assert isinstance(captured["app"], BearerAuthMiddleware)
    assert captured["app"].token_hashes == {digest: "andrew"}
    assert captured["port"] == 8000


def test_main_new_token_prints_and_exits(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["keep-mcp", "--new-token", "marshall"])
    cli.main()
    out = capsys.readouterr().out
    assert "kmcp_" in out
    assert '"marshall":' in out
