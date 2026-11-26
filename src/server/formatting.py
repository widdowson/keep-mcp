"""
Read Keep's rich-text formatting.

Keep stores formatting (bold, italic, underline, headings) in a server-side
document model, not on the LIST_ITEM nodes gkeepapi syncs. A client that
declares the ``CL`` capability gets each changed note's model instead of its
item nodes, as ``serverChanges.snapshot.serializedChunks``: JSON-encoded lists
of commands such as

    ["cbx-add", "<section>", "<item id>", [<position>]]
    ["docs-nestedModel", ["text", 0, "<section>", "<item id>"],
     {"ty": "is", "ibi": 1, "s": "regular text bold text"}]          # insert
    ["docs-nestedModel", ["text", 0, "<section>", "<item id>"],
     {"ty": "as", "st": "text", "si": 14, "ei": 23,
      "sm": {"ts_bd": true, "ts_bd_i": false}}]                      # style

Model indexes are 1-based (index 0 is the model's own start marker) and
``ei`` is inclusive, so a span covers ``text[si - 1:ei]``. Paragraph styles
(``st: "paragraph"``, e.g. ``ps_hd: 2`` for a level-2 heading) apply to the
whole item.

gkeepapi's ordinary sync doesn't declare CL, and its writes (check, move,
edit text) go through the legacy node path; the server keeps the model's
formatting for anything those writes don't touch. So formatting is read with a
separate, read-only CL request and cached per note revision.
"""

import json
import logging

import gkeepapi

logger = logging.getLogger(__name__)

# Text-style flags in the model; the "_i" twins mark inherited values.
_TEXT_STYLES = {"ts_bd": "bold", "ts_it": "italic", "ts_un": "underline", "ts_st": "strikethrough"}


def _iter_commands(serialized_chunks):
    for chunk in serialized_chunks or []:
        try:
            commands = json.loads(chunk)
        except (TypeError, ValueError):
            logger.warning("Unparseable Keep model chunk")
            continue
        yield from commands


def parse_snapshot(serialized_chunks):
    """
    Rebuild each item's text and formatting from a note's model snapshot.

    Returns:
        dict: item id -> {"text": str, "heading": int | None,
        "styles": [set of style names per character], "unknown": [raw sm dicts]}
    """
    items = {}

    def item(item_id):
        return items.setdefault(
            item_id, {"text": "", "heading": None, "styles": [], "unknown": []}
        )

    for command in _iter_commands(serialized_chunks):
        if not isinstance(command, list) or len(command) < 3 or command[0] != "docs-nestedModel":
            continue
        path, op = command[1], command[2]
        if not (isinstance(path, list) and path and path[0] == "text" and isinstance(op, dict)):
            continue
        # Lists address one checkbox item; a plain note has a single body.
        target = item(path[3] if len(path) > 3 else "")
        kind = op.get("ty")
        if kind == "is":
            at = max(int(op.get("ibi", 1)) - 1, 0)
            text = op.get("s", "")
            target["text"] = target["text"][:at] + text + target["text"][at:]
            target["styles"][at:at] = [set() for _ in text]
        elif kind == "ds":
            start, end = int(op.get("si", 1)) - 1, int(op.get("ei", 0))
            del target["styles"][start:end]
            target["text"] = target["text"][:start] + target["text"][end:]
        elif kind == "as":
            style_map = op.get("sm") or {}
            if op.get("st") == "paragraph":
                if "ps_hd" in style_map:
                    target["heading"] = int(style_map["ps_hd"]) or None
                continue
            if op.get("st") != "text":
                target["unknown"].append(op)
                continue
            start, end = int(op.get("si", 1)) - 1, int(op.get("ei", 0))
            for index in range(max(start, 0), min(end, len(target["styles"]))):
                for key, name in _TEXT_STYLES.items():
                    if key in style_map:
                        if style_map[key]:
                            target["styles"][index].add(name)
                        else:
                            target["styles"][index].discard(name)
            if any(key.removesuffix("_i") not in _TEXT_STYLES for key in style_map):
                target["unknown"].append(op)
    return items


def _runs(text, styles):
    run_start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or styles[index] != styles[run_start]:
            yield text[run_start:index], styles[run_start]
            run_start = index


def _wrap(segment, styles):
    # Markdown markers must hug non-space text, so keep edge spaces outside.
    core = segment.strip()
    if not core or not styles:
        return segment
    lead = segment[: len(segment) - len(segment.lstrip())]
    trail = segment[len(segment.rstrip()):]
    if "underline" in styles:
        core = f"<u>{core}</u>"
    if "strikethrough" in styles:
        core = f"~~{core}~~"
    if "bold" in styles and "italic" in styles:
        core = f"***{core}***"
    elif "bold" in styles:
        core = f"**{core}**"
    elif "italic" in styles:
        core = f"*{core}*"
    return lead + core + trail


def describe(parsed):
    """
    Turn one parsed item into the read payload, or None when it's plain.

    Returns:
        dict: {"heading": int | None, "spans": [{"start", "end", "styles"}],
        "markdown": str} with 0-based, end-exclusive span offsets.
    """
    styles = parsed["styles"]
    if parsed["heading"] is None and not any(styles) and not parsed["unknown"]:
        return None
    text = parsed["text"]
    spans = []
    offset = 0
    for segment, segment_styles in _runs(text, styles):
        if segment_styles:
            spans.append(
                {"start": offset, "end": offset + len(segment), "styles": sorted(segment_styles)}
            )
        offset += len(segment)
    markdown = "".join(_wrap(segment, s) for segment, s in _runs(text, styles))
    if parsed["heading"]:
        markdown = "#" * parsed["heading"] + " " + markdown
    result = {"heading": parsed["heading"], "spans": spans, "markdown": markdown}
    if parsed["unknown"]:
        result["unrecognized"] = parsed["unknown"]
    return result


class FormattingReader:
    """Fetch and cache Keep's document models with a read-only CL sync."""

    def __init__(self):
        self._api = None
        self._auth = None
        self._cache = {}  # note id -> (updated timestamp string, {item id: parsed})

    def reset(self):
        self._api = None
        self._auth = None
        self._cache = {}

    def _cl_api(self, keep):
        auth = keep._keep_api.getAuth()
        if self._api is None or self._auth is not auth:
            api = gkeepapi.KeepAPI(auth)
            original = api.send

            def send(**kwargs):
                caps = kwargs["json"]["requestHeader"]["capabilities"]
                caps.append({"type": "CL"})
                return original(**kwargs)

            api.send = send
            self._api, self._auth = api, auth
        return self._api

    def _refresh(self, keep):
        # Never sends nodes: this request only downloads.
        api = self._cl_api(keep)
        cache, version = {}, None
        while True:
            changes = api.changes(target_version=version)
            for raw in changes.get("nodes", []):
                if raw.get("parentId") != "root":
                    continue
                snapshot = (raw.get("serverChanges") or {}).get("snapshot") or {}
                updated = (raw.get("timestamps") or {}).get("updated")
                cache[raw["id"]] = (updated, parse_snapshot(snapshot.get("serializedChunks")))
            version = changes["toVersion"]
            if not changes.get("truncated"):
                break
        self._cache = cache

    def for_notes(self, keep, notes):
        """
        Return {note id: {item id: parsed}} for (note id, updated) pairs.

        updated is the note's timestamps.updated string from the ordinary
        sync. The cache is keyed on it, so a stale model is never served: one
        refetch covers every stale note, and a note still out of step after it
        gets no formatting.
        """
        if any(self._cache.get(note_id, (None,))[0] != updated for note_id, updated in notes):
            self._refresh(keep)
        result = {}
        for note_id, updated in notes:
            cached = self._cache.get(note_id)
            if cached is not None and cached[0] == updated:
                result[note_id] = cached[1]
        return result


reader = FormattingReader()
