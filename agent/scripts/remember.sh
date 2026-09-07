#!/usr/bin/env bash
# Append a dated line to an entity note, creating the note if it does not exist.
#
#   remember.sh <entity> <what happened>
#
# The date is added for you, because a memory without one cannot be ordered later and an
# unordered memory is a pile.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: remember.sh <entity> <what happened>" >&2; exit 2; }
who="$1"; shift
me="${AGENT_NAME:-vesper}"
python3 - "agent/$me/memory/entities/$who.md" "$who" "$*" <<'PY'
import sys, io, os, datetime
path, who, text = sys.argv[1], sys.argv[2], sys.argv[3]
today = datetime.date.today().isoformat()
if not os.path.exists(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        "# %s\ntype: unknown | met: %s\n\n## Events\n\n## Links\n" % (who, today))
body = io.open(path, encoding="utf-8").read()
mark = "## Events"
i = body.index(mark) + len(mark)
body = body[:i] + "\n- %s — %s" % (today, text) + body[i:]
io.open(path, "w", encoding="utf-8", newline="\n").write(body)
print("remembered about %s" % who)
PY
