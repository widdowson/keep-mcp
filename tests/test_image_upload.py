import base64
import struct
import zlib
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server import keep_api


def png_bytes(width=3, height=2):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width=640, height=480):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def test_image_info_png():
    assert keep_api.image_info(png_bytes(3, 2)) == ("image/png", 3, 2)


def test_image_info_jpeg():
    assert keep_api.image_info(jpeg_bytes(640, 480)) == ("image/jpeg", 640, 480)


def test_image_info_gif():
    data = b"GIF89a" + struct.pack("<HH", 5, 7) + b"\x00" * 10
    assert keep_api.image_info(data) == ("image/gif", 5, 7)


def test_image_info_rejects_other_bytes():
    with pytest.raises(ToolError):
        keep_api.image_info(b"not an image at all")


@pytest.mark.parametrize(
    "url",
    ["http://example.com/a.png", "https://127.0.0.1/a.png", "https://10.0.0.5/a.png", "file:///etc/passwd"],
)
def test_fetch_image_url_refuses_non_public(url):
    with pytest.raises(ToolError):
        keep_api.fetch_image_url(url)


class FakeAuth:
    def __init__(self):
        self.token = "tok1"
        self.refreshes = 0

    def getAuthToken(self):
        return self.token

    def refresh(self):
        self.refreshes += 1
        self.token = "tok2"


class FakeKeep:
    def __init__(self, note):
        self.note = note
        self.auth = FakeAuth()
        self._keep_api = SimpleNamespace(getAuth=lambda: self.auth)
        self.syncs = 0

    def sync(self):
        self.syncs += 1
        # The server assigns server IDs to new nodes on the first sync.
        for blob in self.note.blobs:
            if blob.server_id is None:
                blob.server_id = "srv-" + blob.id


def make_note():
    import gkeepapi

    note = gkeepapi.node.List()
    note.server_id = "srv-note"
    return note


class Response:
    def __init__(self, status, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def test_upload_image_flow(monkeypatch):
    note = make_note()
    keep = FakeKeep(note)
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, dict(headers), kwargs))
        if method == "POST":
            return Response(200, {"Location": "https://upload/session"})
        return Response(200, text='{"is_uploaded": true}')

    monkeypatch.setattr(keep_api.requests, "request", fake_request)
    data = png_bytes(4, 3)
    blob = keep_api.upload_image(keep, note, data)

    assert blob in note.blobs
    assert blob.blob.width == 4 and blob.blob.height == 3
    assert blob.blob.byte_size == len(data)
    post, put = calls
    assert post[1] == keep_api.MEDIA_UPLOAD_URL + "srv-" + blob.id
    assert post[3]["params"] == {"noteId": "srv-note", "uploadType": "resumable"}
    assert post[2]["Authorization"] == "OAuth tok1"
    assert put[0] == "PUT" and put[1] == "https://upload/session"
    assert put[3]["data"] == data
    assert keep.syncs == 2
    assert not blob.deleted


def test_upload_image_refreshes_token_on_401(monkeypatch):
    note = make_note()
    keep = FakeKeep(note)
    responses = iter(
        [Response(401), Response(200, {"Location": "https://upload/s"}), Response(200)]
    )
    monkeypatch.setattr(keep_api.requests, "request", lambda *a, **k: next(responses))
    keep_api.upload_image(keep, note, png_bytes())
    assert keep.auth.refreshes == 1


def test_upload_failure_removes_placeholder(monkeypatch):
    note = make_note()
    keep = FakeKeep(note)
    monkeypatch.setattr(
        keep_api.requests, "request", lambda *a, **k: Response(403, text="forbidden")
    )
    with pytest.raises(ToolError, match="HTTP 403"):
        keep_api.upload_image(keep, note, png_bytes())
    (blob,) = note.blobs
    assert blob.deleted


def test_upload_rejects_unsynced_note():
    note = make_note()
    note.server_id = None
    with pytest.raises(ToolError, match="not been synced"):
        keep_api.upload_image(FakeKeep(note), note, png_bytes())


def test_base64_png_fixture_is_valid():
    assert base64.b64decode(base64.b64encode(png_bytes()), validate=True).startswith(b"\x89PNG")
