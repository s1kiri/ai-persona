#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
  pair_python=.venv/bin/python
elif command -v python3.12 >/dev/null 2>&1; then
  pair_python=python3.12
else
  echo 'Нужен Python 3.12 и .venv.' >&2
  exit 1
fi
exec "$pair_python" -m persona_runtime.shared_host "$@"
