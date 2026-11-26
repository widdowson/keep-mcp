"""Print the raw Keep sync JSON for a note and its children. Read-only.

Self-contained (only gkeepapi), so it runs inside an already deployed
keep-mcp container without rebuilding it:

  docker exec -i keep-mcp python - NOTE_ID [EXTRA_CAPABILITY ...] < scripts/dump_raw_nodes.py

GOOGLE_EMAIL and GOOGLE_MASTER_TOKEN come from the environment, as for the
server. The script never sends nodes to the server; it only downloads.

Extra capability codes (e.g. CL) are added to the sync request header. Keep may
only send some fields, such as rich-text formatting, to clients that declare the
matching capability, so a run with and without a code shows the difference.
"""

import json
import os
import sys

import gkeepapi


def full_changes(api, extra_capabilities):
    original = api.send

    def send(**kwargs):
        if extra_capabilities:
            caps = kwargs["json"]["requestHeader"]["capabilities"]
            caps.extend({"type": code} for code in extra_capabilities)
        return original(**kwargs)

    api.send = send
    nodes, version = [], None
    while True:
        changes = api.changes(target_version=version)
        nodes.extend(changes.get("nodes", []))
        version = changes["toVersion"]
        if not changes.get("truncated"):
            return nodes


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    note_id, extra = sys.argv[1], sys.argv[2:]

    keep = gkeepapi.Keep()
    keep.authenticate(os.environ["GOOGLE_EMAIL"], os.environ["GOOGLE_MASTER_TOKEN"], sync=False)
    nodes = full_changes(keep._keep_api, extra)

    wanted = [n for n in nodes if n.get("id") == note_id or n.get("parentId") == note_id]
    if not wanted:
        raise SystemExit(f"No nodes found for {note_id}")
    json.dump({"capabilities_added": extra, "nodes": wanted}, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
