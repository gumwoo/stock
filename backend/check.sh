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
#
# `set -e` carries the failure. The first version of this swallowed it with a
# trailing `|| echo`, which meant the script printed a complaint and exited
# zero — a gate that cannot fail is not a gate, and it was added in the same
# commit that claimed six of them pass.
"$BIN/alembic" check

echo "──── pytest ────"
"$PY" -m pytest -q
echo ""
echo "All gates passed."
