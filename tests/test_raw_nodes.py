"""Raw-field preservation against real gkeepapi node classes."""

import gkeepapi

from server import keep_api, raw_nodes  # keep_api installs the patch

assert keep_api.raw_nodes is raw_nodes

FORMATTING = {"textFormatting": [{"start": 0, "end": 4, "style": "BOLD"}]}


def raw_item(item_id="cbx.1", text="bold text", **extra):
    raw = {
        "id": item_id,
        "kind": "notes#node",
        "type": "LIST_ITEM",
        "parentId": "list.1",
        "parentServerId": "srv.list",
        "serverId": "srv." + item_id,
        "baseVersion": "3",
        "sortValue": "100",
        "text": text,
        "checked": False,
        "superListItemId": None,
        "timestamps": {
            "kind": "notes#timestamps",
            "created": "2026-11-26T19:50:19.234Z",
            "updated": "2026-11-26T19:56:32.302Z",
        },
        "nodeSettings": {
            "newListItemPlacement": "BOTTOM",
            "graveyardState": "COLLAPSED",
            "checkedListItemsPolicy": "GRAVEYARD",
        },
        "annotationsGroup": {"kind": "notes#annotationsGroup"},
        "lastSavedSessionId": "s--1--2",
    }
    raw.update(extra)
    return raw


def load_item(raw):
    item = gkeepapi.node.from_json(raw)
    assert isinstance(item, gkeepapi.node.ListItem)
    return item


def test_unknown_list_item_fields_are_echoed_on_save():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))

    item.checked = True
    saved = item.save()

    assert saved["textFormatting"] == FORMATTING["textFormatting"]
    assert saved["checked"] is True
    # Session-scoped fields are never echoed.
    assert "lastSavedSessionId" not in saved


def test_plain_items_have_no_unmodeled_fields():
    raw_nodes.reset()
    item = load_item(raw_item())
    assert raw_nodes.unmodeled_fields(item) == {}
    assert keep_api.serialize_list_item(item).get("unmodeled_fields") is None


def test_reload_recomputes_extras():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))
    assert raw_nodes.has_unmodeled_fields(item)
    # A second load of the same node from the server, now plain.
    item.load(raw_item())
    assert not raw_nodes.has_unmodeled_fields(item)
    assert "textFormatting" not in item.save()


def test_reload_keeps_extras_when_still_sent():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))
    item.load(raw_item(**FORMATTING))
    assert raw_nodes.unmodeled_fields(item) == FORMATTING


def test_serialize_exposes_unmodeled_fields():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))
    assert keep_api.serialize_list_item(item)["unmodeled_fields"] == FORMATTING


def test_discard_stops_echo():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))
    raw_nodes.discard_unmodeled_fields(item)
    assert "textFormatting" not in item.save()


def test_raw_json_is_a_snapshot_of_what_the_server_sent():
    raw_nodes.reset()
    raw = raw_item(**FORMATTING)
    item = load_item(raw)
    recorded = raw_nodes.raw_json(item)
    assert recorded == raw_item(**FORMATTING)
    recorded["text"] = "changed"
    assert raw_nodes.raw_json(item)["text"] == "bold text"


def test_install_is_idempotent():
    patched = gkeepapi.node.ListItem.save
    raw_nodes.install()
    assert gkeepapi.node.ListItem.save is patched
