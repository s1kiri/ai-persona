#!/usr/bin/env bash
# Set your own next wake, in minutes.
#
#   sleep.sh <minutes>
#
# The character decides this, not a fixed schedule. Somewhere busy and interesting means come back
# soon; nothing happening means come back later. A fixed interval either wastes money on an empty
# room or misses everything that mattered.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: sleep.sh <minutes>" >&2; exit 2; }
me="${AGENT_NAME:-vesper}"
mkdir -p "agent/$me/state"
printf '%s\n' "$1" > "agent/$me/state/sleep.txt"
echo "next wake in $1 minutes"
