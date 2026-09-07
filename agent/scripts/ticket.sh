#!/usr/bin/env bash
# Report something missing or broken to whoever maintains the world.
#
#   ticket.sh "<what you needed, and what for>"
#
# A character that silently works around a missing tool teaches everybody that the tool is not
# needed. Say what you wanted to do, not only what failed.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: ticket.sh \"<what you needed and what for>\"" >&2; exit 2; }
me="${AGENT_NAME:-vesper}"
mkdir -p tickets
python3 - "$me" "$*" <<'PY'
import sys, io, json, datetime, hashlib
who, text = sys.argv[1], sys.argv[2]
tid = hashlib.sha1((who + text).encode("utf-8")).hexdigest()[:8]
rec = {"id": tid, "from": who, "text": text,
       "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "state": "open"}
io.open("tickets/%s.json" % tid, "w", encoding="utf-8", newline="\n").write(
    json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
print("filed %s" % tid)
PY
