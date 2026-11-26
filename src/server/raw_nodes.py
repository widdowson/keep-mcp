"""
Keep the raw sync JSON that gkeepapi does not model.

gkeepapi rebuilds each node from its own model when it saves, so any field
the Keep server sends that gkeepapi does not know about is dropped from the
node it uploads. Keep's rich-text formatting (bold, italic, underline,
headings) is not modelled by gkeepapi 0.17, so editing a formatted list item
can strip a person's formatting.

This module patches gkeepapi in two places:

- Every node load records the raw JSON the server sent, so it can be
  inspected with the get_note_raw tool.
- List items remember the top-level fields gkeepapi ignores and send them
  back unchanged when the item is saved.
"""

import copy
from threading import RLock

from gkeepapi import node as _node

# Fields that describe one sync exchange or that gkeepapi already emits
# conditionally. Echoing them back would be wrong, so they are never kept.
_NEVER_ECHO = frozenset(
    {
        "_dirty",
        "baseVersion",
        "lastSavedSessionId",
        "mergeConflict",
        "moved",
        "serverId",
    }
)

_lock = RLock()
_raw_by_id: dict[str, dict] = {}
_extras_by_id: dict[str, dict] = {}
_original_list_item_save = _node.ListItem.save


def reset():
    """Forget every recorded node, e.g. when a fresh client is built."""
    with _lock:
        _raw_by_id.clear()
        _extras_by_id.clear()


def raw_json(node):
    """Return the last raw JSON the server sent for a node, or None."""
    with _lock:
        raw = _raw_by_id.get(node.id)
        return copy.deepcopy(raw) if raw is not None else None


def unmodeled_fields(node):
    """Return the list-item fields gkeepapi ignores, as sent by the server."""
    with _lock:
        return copy.deepcopy(_extras_by_id.get(node.id, {}))


def has_unmodeled_fields(node):
    with _lock:
        return bool(_extras_by_id.get(node.id))


def discard_unmodeled_fields(node):
    """Stop echoing a list item's unmodelled fields on its next save."""
    with _lock:
        _extras_by_id.pop(node.id, None)


def _modelled_keys(node):
    # The unpatched save, so fields echoed from an earlier load don't count
    # as modelled. clean=False leaves dirty bits alone: no side effects.
    return set(_original_list_item_save(node, clean=False).keys())


def _record(node, raw):
    with _lock:
        _raw_by_id[node.id] = raw
        if isinstance(node, _node.ListItem):
            modelled = _modelled_keys(node)
            extras = {
                key: value
                for key, value in raw.items()
                if key not in modelled and key not in _NEVER_ECHO
            }
            if extras:
                _extras_by_id[node.id] = extras
            else:
                _extras_by_id.pop(node.id, None)


def install():
    """Patch gkeepapi once. Safe to call repeatedly."""
    if getattr(_node.Node, "_keep_mcp_raw_patch", False):
        return

    element_load = _node.Element.load
    list_item_save = _original_list_item_save

    def load(self, raw):
        # gkeepapi mutates parts of the raw dict while loading (labels pop
        # their dirty flag), so record a copy taken before it runs.
        snapshot = copy.deepcopy(raw)
        element_load(self, raw)
        _record(self, snapshot)

    def save(self, clean=True):
        ret = list_item_save(self, clean)
        with _lock:
            extras = _extras_by_id.get(self.id)
            if extras:
                for key, value in extras.items():
                    ret.setdefault(key, copy.deepcopy(value))
        return ret

    _node.Node.load = load
    _node.ListItem.save = save
    _node.Node._keep_mcp_raw_patch = True
