import re
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server import cli


class DummyLabel:
    def __init__(self, label_id="l1", name="keep-mcp"):
        self.id = label_id
        self.name = name


class DummyLabels:
    def __init__(self):
        self._labels = []

    def add(self, label):
        self._labels.append(label)

    def remove(self, label):
        self._labels = [existing for existing in self._labels if existing.id != label.id]

    def all(self):
        return self._labels


class DummyCollaborators:
    def __init__(self):
        self._emails = []

    def all(self):
        return list(self._emails)

    def add(self, email):
        self._emails.append(email)

    def remove(self, email):
        self._emails = [value for value in self._emails if value != email]


class DummyBlobType:
    def __init__(self, value="IMAGE"):
        self.value = value


class DummyBlobInner:
    def __init__(self):
        self.type = DummyBlobType("IMAGE")


class DummyBlob:
    def __init__(self, blob_id="b1"):
        self.id = blob_id
        self.blob = DummyBlobInner()


class DummyItem:
    def __init__(self, item_id: str, text: str, checked: bool):
        self.id = item_id
        self.text = text
        self.checked = checked
        self.parent_item = None

    def indent(self, child):
        child.parent_item = self

    def dedent(self, child):
        child.parent_item = None

    def delete(self):
        self.deleted = True


class DummyNote:
    def __init__(self, note_id="n1"):
        self.id = note_id
        self.title = "title"
        self.text = "text"
        self.pinned = False
        self.archived = False
        self.trashed = False
        self.type = type("T", (), {"value": "NOTE"})()
        self.color = type("C", (), {"value": "white"})()
        self.labels = DummyLabels()
        self.collaborators = DummyCollaborators()
        self.blobs = [DummyBlob()]
        self.deleted = False

    def delete(self):
        self.deleted = True

    def trash(self):
        self.trashed = True

    def untrash(self):
        self.trashed = False

    def undelete(self):
        self.deleted = False


class DummyList(DummyNote):
    def __init__(self, note_id="list1"):
        super().__init__(note_id)
        self.type = type("T", (), {"value": "LIST"})()
        self.items = []

    def add(self, text, checked=False):
        item = DummyItem(f"i{len(self.items)+1}", text, checked)
        self.items.append(item)
        return item

    def get(self, item_id):
        for item in self.items:
            if item.id == item_id:
                return item
        return None


class DummyKeep:
    def __init__(self):
        self.notes = {}
        self._labels = {"l1": DummyLabel("l1", "keep-mcp")}
        self.sync_calls = 0

    def sync(self):
        self.sync_calls += 1

    def find(self, **kwargs):
        self.last_find_kwargs = kwargs
        return list(self.notes.values())

    def get(self, note_id):
        return self.notes.get(note_id)

    def createNote(self, title=None, text=None):
        note = DummyNote("created")
        note.title = title
        note.text = text
        self.notes[note.id] = note
        return note

    def createList(self, title=None, items=None):
        note = DummyList("created_list")
        note.title = title
        if items:
            for text, checked in items:
                note.add(text, checked)
        self.notes[note.id] = note
        return note

    def findLabel(self, name):
        for label in self._labels.values():
            if label.name == name:
                return label
        return None

    def createLabel(self, name):
        label = DummyLabel("new", name)
        self._labels[label.id] = label
        return label

    def labels(self):
        return list(self._labels.values())

    def getLabel(self, label_id):
        return self._labels.get(label_id)

    def all(self):
        return list(self.notes.values())

    def deleteLabel(self, label_id):
        self._labels.pop(label_id, None)

    def getMediaLink(self, blob):
        return f"https://media/{blob.id}"


@pytest.fixture()
def keep(monkeypatch):
    keep = DummyKeep()
    keep.notes["n1"] = DummyNote("n1")
    keep.notes["n1"].labels.add(DummyLabel("l1", "keep-mcp"))
    keep.notes["list1"] = DummyList("list1")
    keep.notes["list1"].labels.add(DummyLabel("l1", "keep-mcp"))
    keep.notes["list1"].add("existing", checked=False)

    monkeypatch.setattr(cli, "get_client", lambda: keep)
    monkeypatch.setattr(cli.gkeepapi.node, "List", DummyList)
    monkeypatch.setattr(
        cli.gkeepapi.node,
        "ColorValue",
        lambda color: type("Color", (), {"value": color})(),
    )
    return keep


def test_find_forwards_filters(keep):
    result = cli.find(
            query="q",
            labels=["l1"],
            colors=["red"],
            pinned=True,
            archived=False,
            trashed=False,
        )
    assert keep.last_find_kwargs["query"].pattern == "q"
    assert keep.last_find_kwargs["labels"] == ["l1"]
    assert [color.value for color in keep.last_find_kwargs["colors"]] == ["red"]
    assert isinstance(result, list)


def test_find_without_colors_passes_none(keep):
    cli.find(query="q")
    assert keep.last_find_kwargs["colors"] is None


def test_find_query_case_insensitive_by_default(keep):
    cli.find(query="Hello")
    pattern = keep.last_find_kwargs["query"]
    assert isinstance(pattern, re.Pattern)
    assert pattern.search("say HELLO there")
    assert pattern.search("no match here") is None


def test_find_query_case_sensitive_opt_in(keep):
    cli.find(query="Hello", case_sensitive=True)
    assert keep.last_find_kwargs["query"] == "Hello"


def test_find_empty_query_and_no_dates_pass_through(keep):
    cli.find()
    assert keep.last_find_kwargs["query"] == ""
    assert keep.last_find_kwargs["func"] is None


def test_find_date_filter_func(keep):
    cli.find(created_after="2026-07-01", updated_before="2026-08-01T00:00:00Z")
    within = keep.last_find_kwargs["func"]

    def note_with(created, updated):
        return SimpleNamespace(
            timestamps=SimpleNamespace(created=created, updated=updated)
        )

    utc = timezone.utc
    assert within(note_with(datetime(2026, 7, 15, tzinfo=utc), datetime(2026, 7, 20, tzinfo=utc))) is True
    assert within(note_with(datetime(2026, 6, 15, tzinfo=utc), datetime(2026, 7, 20, tzinfo=utc))) is False
    assert within(note_with(datetime(2026, 7, 15, tzinfo=utc), datetime(2026, 8, 2, tzinfo=utc))) is False
    # Naive timestamps (older gkeepapi) are treated as UTC, not an error.
    assert within(note_with(datetime(2026, 7, 15), datetime(2026, 7, 20))) is True
    assert within(SimpleNamespace(timestamps=None)) is False


def test_find_invalid_date_raises(keep):
    with pytest.raises(ToolError, match="created_after"):
        cli.find(created_after="not-a-date")


def test_find_limit_caps_results(keep):
    assert len(cli.find()) == 2
    assert len(cli.find(limit=1)) == 1
    assert cli.find(limit=0) == []


def test_find_returns_50_notes_by_default(keep):
    for i in range(60):
        keep.notes[f"extra{i}"] = DummyNote(f"extra{i}")
    assert len(cli.find()) == 50
    assert len(cli.find(limit=None)) == 62


def test_find_invalid_color_raises(keep):
    with pytest.raises(ToolError, match="colors"):
        anyio.run(cli.mcp.call_tool, "find", {"colors": ["invalid"]})


def test_get_note(keep):
    data = cli.get_note("n1")
    assert data["id"] == "n1"


def test_create_note_labels_and_sync(keep):
    data = cli.create_note("t", "body")
    assert data["id"] == "created"
    assert keep.sync_calls == 1


def test_create_note_creates_label_when_missing(keep):
    keep._labels = {}
    data = cli.create_note("t", "body")
    assert data["labels"][0]["name"] == "keep-mcp"


def test_create_list_variants(keep):
    data = cli.create_list("list")
    assert data["id"] == "created_list"
    data_with_items = cli.create_list("list", items=[{"text": "a", "checked": True}])
    assert data_with_items["items"][0]["checked"] is True


def test_create_list_creates_label_when_missing(keep):
    keep._labels = {}
    data = cli.create_list("list")
    assert data["labels"][0]["name"] == "keep-mcp"


def test_update_note_updates_fields(keep):
    data = cli.update_note("n1", title="new", text="changed")
    assert data["title"] == "new"
    assert data["text"] == "changed"


def test_anticipated_errors_reach_the_client(keep):
    with pytest.raises(ToolError, match="Note with ID missing not found"):
        anyio.run(cli.mcp.call_tool, "get_note", {"note_id": "missing"})


def test_tools_return_structured_content(keep):
    result = anyio.run(cli.mcp.call_tool, "get_note", {"note_id": "n1"})
    assert result.structured_content["id"] == "n1"


def test_update_note_not_found_raises(keep):
    with pytest.raises(ToolError, match="not found"):
        cli.update_note("missing", title="x")


def test_list_item_roundtrip(keep):
    add = cli.add_list_item("list1", "task", checked=True)
    item_id = add["item_id"]
    updated = cli.update_list_item("list1", item_id, text="task2", checked=False)
    assert any(item["id"] == item_id for item in updated["items"])


def test_update_list_item_missing_item_raises(keep):
    with pytest.raises(ToolError, match="not found"):
        cli.update_list_item("list1", "missing", text="x")


def test_delete_list_item_paths(keep):
    with pytest.raises(ToolError, match="not found"):
        cli.delete_list_item("list1", "missing")

    new_item_id = cli.add_list_item("list1", "task")["item_id"]
    data = cli.delete_list_item("list1", new_item_id)
    assert "marked for deletion" in data["message"]


def test_list_item_requires_list_type(keep):
    with pytest.raises(ToolError, match="not a list"):
        cli.add_list_item("n1", "x")
    with pytest.raises(ToolError, match="not a list"):
        cli.update_list_item("n1", "i1", text="x")
    with pytest.raises(ToolError, match="not a list"):
        cli.delete_list_item("n1", "i1")


def test_set_note_color_validates(keep):
    with pytest.raises(ToolError, match="color"):
        anyio.run(cli.mcp.call_tool, "set_note_color", {"note_id": "n1", "color": "invalid"})


def test_note_state_transitions(keep):
    assert cli.set_note_color("n1", "red")["color"] == "red"
    assert cli.pin_note("n1", True)["pinned"] is True
    assert cli.archive_note("n1", True)["archived"] is True
    assert cli.trash_note("n1")["trashed"] is True
    assert cli.restore_note("n1")["trashed"] is False


def test_delete_note_marks_deleted(keep):
    msg = cli.delete_note("n1")
    assert "marked for deletion" in msg["message"]


def test_label_crud_and_missing_label_errors(keep):
    labels = cli.list_labels()
    assert labels

    created = cli.create_label("other")
    assert created["name"] == "other"
    message = cli.delete_label(created["id"])
    assert "marked for deletion" in message["message"]

    with pytest.raises(ToolError, match="Label with ID bad not found"):
        cli.delete_label("bad")
    with pytest.raises(ToolError, match="Label with ID bad not found"):
        cli.add_label_to_note("n1", "bad")
    with pytest.raises(ToolError, match="Label with ID bad not found"):
        cli.remove_label_from_note("n1", "bad")


def test_delete_label_safe_mode_guards(keep, monkeypatch):
    # Deleting the keep-mcp label is blocked in safe mode.
    with pytest.raises(ToolError, match="keep-mcp"):
        cli.delete_label("l1")

    # Deleting a label that is on an unmanaged note is also blocked in safe mode.
    unmanaged = DummyNote("unmanaged")  # no keep-mcp label
    shared = DummyLabel("shared", "shared-tag")
    keep._labels["shared"] = shared
    unmanaged.labels.add(shared)
    keep.notes["unmanaged"] = unmanaged
    with pytest.raises(ToolError, match="unmanaged"):
        cli.delete_label("shared")

    # UNSAFE_MODE bypasses both guards.
    monkeypatch.setenv("UNSAFE_MODE", "true")
    msg = cli.delete_label("shared")
    assert "marked for deletion" in msg["message"]


def test_label_add_remove(keep):
    keep._labels["l2"] = DummyLabel("l2", "other")
    add = cli.add_label_to_note("n1", "l2")
    assert any(label["id"] == "l2" for label in add["labels"])
    remove = cli.remove_label_from_note("n1", "l2")
    assert all(label["id"] != "l2" for label in remove["labels"])


def test_collaborator_list_add_remove(keep):
    before = cli.list_note_collaborators("n1")
    assert before == []

    data = cli.add_note_collaborator("n1", "user@example.com")
    assert "user@example.com" in data["collaborators"]
    after = cli.remove_note_collaborator("n1", "user@example.com")
    assert "user@example.com" not in after["collaborators"]


def test_list_note_media(keep):
    media = cli.list_note_media("n1")
    assert media[0]["media_link"].startswith("https://media/")


def test_download_media_writes_files(keep, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "fetch_blob_bytes", lambda k, b: (b"png-bytes", "image/png"))
    saved = cli.download_media("n1", str(tmp_path))
    assert saved[0]["blob_id"] == "b1"
    assert saved[0]["type"] == "IMAGE"
    assert saved[0]["path"].endswith("b1.png")
    assert saved[0]["bytes"] == len(b"png-bytes")
    with open(saved[0]["path"], "rb") as fh:
        assert fh.read() == b"png-bytes"


def test_download_media_unknown_blob(keep, tmp_path):
    with pytest.raises(ToolError, match="Blob with ID nope not found"):
        cli.download_media("n1", str(tmp_path), blob_id="nope")


def test_modification_guard_blocks_when_unlabeled(keep, monkeypatch):
    keep.notes["n1"].labels = DummyLabels()
    monkeypatch.setattr(cli, "can_modify_note", lambda _: False)
    with pytest.raises(ToolError, match="cannot be modified"):
        cli.update_note("n1", title="x")


def test_create_list_with_parent_item_id(keep):
    items = [
        {"text": "parent", "checked": False},
        {"text": "child", "checked": False, "parent_item_id": "i1"},
    ]
    data = cli.create_list("nested", items=items)
    child = next(i for i in data["items"] if i["text"] == "child")
    assert child["parent_item_id"] == "i1"


def test_create_list_invalid_parent_item_id_raises(keep):
    items = [
        {"text": "child", "checked": False, "parent_item_id": "nonexistent"},
    ]
    with pytest.raises(ToolError, match="parent_item_id 'nonexistent' not found"):
        cli.create_list("bad", items=items)


def test_add_list_item_with_parent(keep):
    add = cli.add_list_item("list1", "sub", parent_item_id="i1")
    item_id = add["item_id"]
    item = keep.notes["list1"].get(item_id)
    assert item.parent_item is not None
    assert item.parent_item.id == "i1"


def test_add_list_item_invalid_parent_raises(keep):
    with pytest.raises(ToolError, match="Parent item with ID nonexistent not found"):
        cli.add_list_item("list1", "sub", parent_item_id="nonexistent")


def test_update_list_item_reparent(keep):
    # Add two items then reparent the second under the first.
    add1 = cli.add_list_item("list1", "a")
    add2 = cli.add_list_item("list1", "b")
    data = cli.update_list_item("list1", add2["item_id"], parent_item_id=add1["item_id"])
    child = next(i for i in data["items"] if i["id"] == add2["item_id"])
    assert child["parent_item_id"] == add1["item_id"]


def test_update_list_item_dedent(keep):
    # Add an item as a child then dedent it.
    add = cli.add_list_item("list1", "child", parent_item_id="i1")
    item_id = add["item_id"]
    data = cli.update_list_item("list1", item_id, parent_item_id="")
    child = next(i for i in data["items"] if i["id"] == item_id)
    assert child["parent_item_id"] is None


def test_update_list_item_invalid_parent_raises(keep):
    add = cli.add_list_item("list1", "x")
    with pytest.raises(ToolError, match="Parent item with ID bad not found"):
        cli.update_list_item("list1", add["item_id"], parent_item_id="bad")


def test_main_runs_stdio_transport(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        captured["called"] = True

    monkeypatch.setattr(cli.mcp, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["keep-mcp"])
    cli.main()
    assert captured.get("called") is True
    assert captured.get("transport", "stdio") == "stdio"


def test_main_runs_streamable_http_transport(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)

    removed = []
    monkeypatch.setattr(cli.mcp, "run", fake_run)
    monkeypatch.setattr(cli.mcp, "remove_tool", removed.append)
    monkeypatch.setattr(
        "sys.argv",
        ["keep-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "9000"],
    )
    cli.main()
    assert removed == ["download_media"]
    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000


# --- Rich-text formatting guards and images -----------------------------------


@pytest.fixture()
def formatted(keep, monkeypatch):
    formatted_ids = {"i1"}
    monkeypatch.setattr(cli.raw_nodes, "has_unmodeled_fields", lambda item: item.id in formatted_ids)
    monkeypatch.setattr(cli.raw_nodes, "discard_unmodeled_fields", lambda item: formatted_ids.discard(item.id))
    return formatted_ids


def test_update_list_item_refuses_text_change_on_formatted_item(keep, formatted):
    with pytest.raises(ToolError, match="discard_formatting"):
        cli.update_list_item("list1", "i1", text="new")
    assert keep.notes["list1"].items[0].text == "existing"
    assert keep.sync_calls == 0


def test_update_list_item_checks_formatted_item(keep, formatted):
    cli.update_list_item("list1", "i1", checked=True)
    assert keep.notes["list1"].items[0].checked is True
    assert formatted == {"i1"}


def test_update_list_item_same_text_is_not_a_change(keep, formatted):
    cli.update_list_item("list1", "i1", text="existing", checked=True)
    assert formatted == {"i1"}


def test_update_list_item_discard_formatting(keep, formatted):
    cli.update_list_item("list1", "i1", text="new", discard_formatting=True)
    assert keep.notes["list1"].items[0].text == "new"
    assert formatted == set()


def test_replace_list_items_refuses_formatted_list(keep, formatted, monkeypatch):
    keep.notes["list1"].items[0].super_list_item_id = None
    with pytest.raises(ToolError, match="discard_formatting"):
        cli.replace_list_items("list1", [{"text": "a"}])
    assert keep.notes["list1"].items[0].text == "existing"


def test_get_note_raw(keep, monkeypatch):
    note = keep.notes["list1"]
    note.children = [SimpleNamespace(id="i1", type=SimpleNamespace(value="LIST_ITEM"))]
    monkeypatch.setattr(cli.raw_nodes, "raw_json", lambda node: {"id": node.id, "x": 1})
    result = cli.get_note_raw("list1")
    assert result["note"] == {"id": "list1", "x": 1}
    assert result["children"] == [{"id": "i1", "type": "LIST_ITEM", "raw": {"id": "i1", "x": 1}}]


def test_add_note_image_requires_exactly_one_source(keep):
    with pytest.raises(ToolError, match="exactly one"):
        cli.add_note_image("n1")
    with pytest.raises(ToolError, match="exactly one"):
        cli.add_note_image("n1", image_base64="eA==", image_url="https://x/y.png")


def test_add_note_image_rejects_bad_base64(keep):
    with pytest.raises(ToolError, match="base64"):
        cli.add_note_image("n1", image_base64="not base64!!")


def test_add_note_image_replaces_after_upload(keep, monkeypatch):
    note = keep.notes["n1"]
    old = note.blobs[0]
    old.delete = lambda: setattr(old, "deleted", True)
    new_blob = SimpleNamespace(id="b2", blob=None)
    uploads = []

    def fake_upload(k, n, data):
        uploads.append(data)
        assert not getattr(old, "deleted", False)
        return new_blob

    monkeypatch.setattr(cli, "upload_image", fake_upload)
    monkeypatch.setattr(cli, "serialize_blob", lambda b: {"blob_id": b.id})
    result = cli.add_note_image("n1", image_base64="aGVsbG8=", replace_blob_id="b1")
    assert uploads == [b"hello"]
    assert old.deleted is True
    assert result["blob"] == {"blob_id": "b2"}


def test_add_note_image_unknown_replace_blob(keep, monkeypatch):
    monkeypatch.setattr(cli, "upload_image", lambda *a: pytest.fail("should not upload"))
    with pytest.raises(ToolError, match="not found"):
        cli.add_note_image("n1", image_base64="aGVsbG8=", replace_blob_id="nope")


def test_add_note_image_respects_safe_mode(keep, monkeypatch):
    monkeypatch.setattr(cli, "can_modify_note", lambda _: False)
    with pytest.raises(ToolError, match="cannot be modified"):
        cli.add_note_image("n1", image_base64="aGVsbG8=")
