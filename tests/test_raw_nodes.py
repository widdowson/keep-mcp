"""Raw sync JSON recording against real gkeepapi node classes."""

import gkeepapi

from server import keep_api, raw_nodes  # keep_api installs the patch

assert keep_api.raw_nodes is raw_nodes

FORMATTING = {"extraField": [{"start": 0, "end": 4}]}


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


def test_raw_json_is_a_snapshot_of_what_the_server_sent():
    raw_nodes.reset()
    raw = raw_item(**FORMATTING)
    item = load_item(raw)
    recorded = raw_nodes.raw_json(item)
    assert recorded == raw_item(**FORMATTING)
    recorded["text"] = "changed"
    assert raw_nodes.raw_json(item)["text"] == "bold text"


def test_install_is_idempotent():
    patched = gkeepapi.node.Node.load
    raw_nodes.install()
    assert gkeepapi.node.Node.load is patched


def test_save_is_untouched():
    raw_nodes.reset()
    item = load_item(raw_item(**FORMATTING))
    assert "extraField" not in item.save()
