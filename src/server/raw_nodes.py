"""
Record the raw sync JSON the Keep server sends for each node.

gkeepapi keeps only the fields it models. The raw JSON is what the
get_note_raw tool shows, for diagnosing fields gkeepapi ignores.
"""

import copy
from threading import RLock

from gkeepapi import node as _node

_lock = RLock()
_raw_by_id: dict[str, dict] = {}


def reset():
    """Forget every recorded node, e.g. when a fresh client is built."""
    with _lock:
        _raw_by_id.clear()


def raw_json(node):
    """Return the last raw JSON the server sent for a node, or None."""
    with _lock:
        raw = _raw_by_id.get(node.id)
        return copy.deepcopy(raw) if raw is not None else None


def install():
    """Patch gkeepapi's node loading once. Safe to call repeatedly."""
    if getattr(_node.Node, "_keep_mcp_raw_patch", False):
        return

    element_load = _node.Element.load

    def load(self, raw):
        # gkeepapi mutates parts of the raw dict while loading (labels pop
        # their dirty flag), so record a copy taken before it runs.
        snapshot = copy.deepcopy(raw)
        element_load(self, raw)
        with _lock:
            _raw_by_id[self.id] = snapshot

    _node.Node.load = load
    _node.Node._keep_mcp_raw_patch = True
