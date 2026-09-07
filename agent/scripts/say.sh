#!/usr/bin/env bash
# Append one message to your outbox.
#
#   say.sh <recipient> <text>
#
# One JSON object per line. The runner picks the file up, sends what is in it, and does not care
# how it got there. That is the whole interface, and it is a file on purpose: a character can
# write a file when a network is down, and the message survives a crashed session.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: say.sh <recipient> <text>" >&2; exit 2; }
to="$1"; shift
me="${AGENT_NAME:-vesper}"
out="agent/$me/outbox.jsonl"
python3 - "$out" "$to" "$*" <<'PY'
import json, sys, datetime, io
out, to, text = sys.argv[1], sys.argv[2], sys.argv[3]
line = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "to": to, "text": text}
with io.open(out, "a", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
print("queued to %s" % to)
PY
