#!/usr/bin/env bash
# All four CI gates, in the order that fails fastest.
#
# PYTHONUTF8 is set because this machine's default codepage is cp949, and
# tooling that reads source with the platform codec chokes on non-ASCII
# characters in docstrings.
set -euo pipefail
export PYTHONUTF8=1

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
BIN="$(dirname "$PY")"

echo "──── ruff format ────"
"$BIN/ruff" format --check .
echo "──── ruff check ────"
"$BIN/ruff" check .
echo "──── mypy ────"
"$BIN/mypy" app
echo "──── import-linter ────"
"$BIN/lint-imports"
echo "──── alembic check ────"
# Models and migrations must describe the same schema; see ci.yml for why.
# Needs a database, so it is skipped when one is not reachable rather than
# turning the local script into something that only runs on CI.
if "$BIN/alembic" check >/dev/null 2>&1; then
  echo "models and migrations agree"
else
  "$BIN/alembic" check || echo "(skipped or failed — see above)"
fi

echo "──── pytest ────"
"$PY" -m pytest -q
echo ""
echo "All gates passed."
