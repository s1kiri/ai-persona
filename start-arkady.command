#!/bin/sh
# Standalone Arkady. The default only opens a local preparation panel.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
  arkady_python=.venv/bin/python
elif command -v python3.12 >/dev/null 2>&1; then
  arkady_python=python3.12
else
  echo 'Нужен Python 3.12: создайте .venv командой python3.12 -m venv .venv.' >&2
  exit 1
fi
arkady_mode=${1-lab}
if [ "$#" -gt 0 ]; then shift; fi
case "$arkady_mode" in
  lab)
    exec "$arkady_python" -m persona_runtime.world_lab \
      --profile characters/arkady/character.json --data-dir .local/arkady-lab --port 8866 \
      --personal-life --active-world --autonomous-posts --direct-channel "$@"
    ;;
  setup)
    exec "$arkady_python" -m persona_runtime.telegram_setup \
      --profile characters/arkady/character.json --data-dir .local/arkady-telegram --port 8867 "$@"
    ;;
  live)
    if "$arkady_python" - <<'PY'
from pathlib import Path
import sqlite3
import sys
path = Path('.local/arkady-life/world.sqlite3')
if not path.exists():
    sys.exit(0)
with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
    initialized = db.execute('SELECT 1 FROM world_meta WHERE key=?', ('actor:arkady:channel_epoch',)).fetchone()
sys.exit(1 if initialized else 0)
PY
    then
      set -- --fresh "$@"
    fi
    exec "$arkady_python" -m persona_runtime.channel_life \
      --profile characters/arkady/character.json --data-dir .local/arkady-life \
      --telegram-dir .local/arkady-telegram --world-port 8866 --telegram-port 8867 "$@"
    ;;
  *)
    echo 'Использование: ./start-arkady.command [lab|setup|live] [аргументы]' >&2
    exit 2
    ;;
esac
