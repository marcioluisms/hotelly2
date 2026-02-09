#!/usr/bin/env bash
set -euo pipefail

echo "[verify] python compileall"
uv run python -m compileall -q src tests

echo "[verify] pytest"
uv run pytest -q

echo "[verify] (optional) ruff"
if uv run python -c "import ruff" >/dev/null 2>&1; then
  uv run ruff check .
else
  echo "[verify] ruff not installed; skipping"
fi

echo "[verify] (optional) alembic upgrade heads"
if uv run python -c "import alembic" >/dev/null 2>&1; then
  if [ -n "${DATABASE_URL:-}" ]; then
    uv run alembic upgrade heads
  else
    echo "[verify] DATABASE_URL not set; skipping migrations"
  fi
else
  echo "[verify] alembic not installed; skipping migrations"
fi

echo "[verify] done"
