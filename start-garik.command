#!/bin/sh
# Double-click on macOS. The model runs remotely; only the local runtime stays here.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m persona_runtime chat "$@"
fi
exec python3 -m persona_runtime chat "$@"
