"""Formatting parsed from a real CL snapshot of Keep's document model."""

import json

from server import formatting, keep_api

# serverChanges.snapshot.serializedChunks for "Roberta's to-dos" (test items).
CHUNK = json.dumps([
    ["sct-add", 0, "sct.q", "cbx"],
    ["cbx-add", "sct.q", "plain", [0]],
    ["docs-nestedModel", ["text", 0, "sct.q", "plain"],
     {"ibi": 1, "s": "Send Andrew the Citi statements [sdfsd] https://walcer.family/foo ", "ty": "is"}],
    ["cbx-add", "sct.q", "waiting", [1]],
    ["docs-nestedModel", ["text", 0, "sct.q", "waiting"], {"ibi": 1, "s": "──── WAITING ────", "ty": "is"}],
    ["docs-nestedModel", ["text", 0, "sct.q", "waiting"],
     {"st": "paragraph", "ei": 18, "ty": "as", "si": 18, "sm": {"ps_hdid": "h.ebea6brrm1u8", "ps_hd": 1}}],
    ["cbx-add", "sct.q", "cbx.h5posoapj2sw", [4]],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.h5posoapj2sw"], {"ibi": 1, "s": "h2", "ty": "is"}],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.h5posoapj2sw"],
     {"st": "paragraph", "ei": 3, "ty": "as", "si": 3, "sm": {"ps_hdid": "h.sago2dbpt21w", "ps_hd": 2}}],
    ["cbx-add", "sct.q", "cbx.46drfvnpmhbc", [5]],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.46drfvnpmhbc"],
     {"ibi": 1, "s": "regular text bold text italic underline", "ty": "is"}],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.46drfvnpmhbc"],
     {"st": "text", "ei": 23, "ty": "as", "si": 14, "sm": {"ts_bd_i": False, "ts_bd": True}}],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.46drfvnpmhbc"],
     {"st": "text", "ei": 30, "ty": "as", "si": 24,
      "sm": {"ts_it": True, "ts_bd_i": True, "ts_it_i": False, "ts_bd": True}}],
    ["docs-nestedModel", ["text", 0, "sct.q", "cbx.46drfvnpmhbc"],
     {"st": "text", "ei": 39, "ty": "as", "si": 31,
      "sm": {"ts_un": True, "ts_un_i": False, "ts_it": True, "ts_bd_i": True, "ts_it_i": True, "ts_bd": True}}],
])


def parsed():
    return formatting.parse_snapshot([CHUNK])


def test_texts_rebuilt():
    items = parsed()
    assert items["cbx.46drfvnpmhbc"]["text"] == "regular text bold text italic underline"
    assert items["cbx.h5posoapj2sw"]["text"] == "h2"


def test_plain_item_has_no_formatting():
    assert formatting.describe(parsed()["plain"]) is None


def test_headings():
    items = parsed()
    assert formatting.describe(items["cbx.h5posoapj2sw"]) == {
        "heading": 2, "spans": [], "markdown": "## h2"
    }
    assert formatting.describe(items["waiting"])["markdown"] == "# ──── WAITING ────"


def test_inline_styles():
    info = formatting.describe(parsed()["cbx.46drfvnpmhbc"])
    assert info["heading"] is None
    assert info["spans"] == [
        {"start": 13, "end": 23, "styles": ["bold"]},
        {"start": 23, "end": 30, "styles": ["bold", "italic"]},
        {"start": 30, "end": 39, "styles": ["bold", "italic", "underline"]},
    ]
    assert info["markdown"] == "regular text **bold text** ***italic*** ***<u>underline</u>***"
    assert "unrecognized" not in info


def test_style_can_be_turned_off_and_unknown_styles_surface():
    chunk = json.dumps([
        ["docs-nestedModel", ["text", 0, "s", "x"], {"ibi": 1, "s": "abcd", "ty": "is"}],
        ["docs-nestedModel", ["text", 0, "s", "x"], {"st": "text", "si": 1, "ei": 4, "ty": "as", "sm": {"ts_bd": True}}],
        ["docs-nestedModel", ["text", 0, "s", "x"], {"st": "text", "si": 3, "ei": 4, "ty": "as", "sm": {"ts_bd": False}}],
        ["docs-nestedModel", ["text", 0, "s", "x"], {"st": "text", "si": 1, "ei": 1, "ty": "as", "sm": {"lnks_link": {"ulnk_url": "u"}}}],
    ])
    info = formatting.describe(formatting.parse_snapshot([chunk])["x"])
    assert info["markdown"] == "**ab**cd"
    assert info["unrecognized"][0]["sm"] == {"lnks_link": {"ulnk_url": "u"}}


def test_delete_op():
    chunk = json.dumps([
        ["docs-nestedModel", ["text", 0, "s", "x"], {"ibi": 1, "s": "hello world", "ty": "is"}],
        ["docs-nestedModel", ["text", 0, "s", "x"], {"si": 6, "ei": 11, "ty": "ds"}],
    ])
    assert formatting.parse_snapshot([chunk])["x"]["text"] == "hello"


def test_bad_chunks_are_skipped():
    assert formatting.parse_snapshot(["not json", json.dumps([["x"]])]) == {}


class FakeApi:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def changes(self, target_version=None):
        self.calls += 1
        return self.pages.pop(0)


def cl_node(note_id, updated, chunks=None):
    node = {"id": note_id, "parentId": "root", "timestamps": {"updated": updated}}
    if chunks is not None:
        node["serverChanges"] = {"snapshot": {"serializedChunks": chunks}}
    return node


def test_reader_caches_until_note_changes(monkeypatch):
    reader = formatting.FormattingReader()
    pages = [
        {"nodes": [cl_node("n1", "t1", [CHUNK]), cl_node("n2", "t1")], "toVersion": "1", "truncated": False},
        {"nodes": [cl_node("n1", "t2", [CHUNK])], "toVersion": "2", "truncated": False},
    ]
    api = FakeApi(pages)
    monkeypatch.setattr(reader, "_cl_api", lambda keep: api)

    first = reader.for_notes(None, [("n1", "t1"), ("n2", "t1")])
    assert "cbx.46drfvnpmhbc" in first["n1"] and first["n2"] == {}
    reader.for_notes(None, [("n1", "t1"), ("n2", "t1")])
    assert api.calls == 1  # served from cache, even for the plain note
    assert "cbx.46drfvnpmhbc" in reader.for_notes(None, [("n1", "t2")])["n1"]
    assert api.calls == 2


def test_reader_refetches_then_uses_fresh_model_despite_timestamp_skew(monkeypatch):
    reader = formatting.FormattingReader()
    reader._cache = {"n1": ("old", {})}
    api = FakeApi([{"nodes": [cl_node("n1", "skewed", [CHUNK])], "toVersion": "1", "truncated": False}])
    monkeypatch.setattr(reader, "_cl_api", lambda keep: api)
    result = reader.for_notes(None, [("n1", "new")])
    assert api.calls == 1
    assert "cbx.46drfvnpmhbc" in result["n1"]


def test_reader_unknown_note_gets_nothing(monkeypatch):
    reader = formatting.FormattingReader()
    api = FakeApi([{"nodes": [], "toVersion": "1", "truncated": False}])
    monkeypatch.setattr(reader, "_cl_api", lambda keep: api)
    assert reader.for_notes(None, [("n1", "t")]) == {}


def test_cl_api_adds_capability(monkeypatch):
    sent = []

    class Auth:
        pass

    keep = type("K", (), {"_keep_api": type("A", (), {"getAuth": lambda self: Auth})()})()
    reader = formatting.FormattingReader()
    api = reader._cl_api(keep)
    monkeypatch.setattr(api, "_send", lambda **kw: sent.append(kw) or type("R", (), {"json": lambda self: {"toVersion": "1"}})())
    api.changes()
    caps = [c["type"] for c in sent[0]["json"]["requestHeader"]["capabilities"]]
    assert "CL" in caps
    assert sent[0]["json"]["nodes"] == []
    assert reader._cl_api(keep) is api


class Item:
    def __init__(self, item_id, text):
        self.id, self.text = item_id, text


class Note:
    def __init__(self, items):
        self.id = "n1"
        self.items = items


def test_note_formatting_skips_text_mismatch(monkeypatch):
    note = Note([Item("cbx.46drfvnpmhbc", "regular text bold text italic underline"),
                 Item("cbx.h5posoapj2sw", "h2 edited")])
    monkeypatch.setattr(keep_api, "_raw_updated", lambda n: "t1")
    monkeypatch.setattr(keep_api.formatting.reader, "for_notes", lambda keep, pairs: {"n1": parsed()})
    result = keep_api.note_formatting(None, [note])["n1"]
    assert set(result) == {"cbx.46drfvnpmhbc"}


def test_note_formatting_failure(monkeypatch):
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    note = Note([Item("a", "x")])
    monkeypatch.setattr(keep_api, "_raw_updated", lambda n: "t1")

    def boom(keep, pairs):
        raise RuntimeError("network down")

    monkeypatch.setattr(keep_api.formatting.reader, "for_notes", boom)
    assert keep_api.note_formatting(None, [note]) == {}
    monkeypatch.setattr(keep_api.formatting.reader, "for_notes", lambda keep, pairs: {"n1": {"a": None}})
    assert keep_api.note_formatting(None, [note]) == {}  # a parse failure is contained too
    with pytest.raises(ToolError, match="discard_formatting"):
        keep_api.note_formatting(None, [note], strict=True)
