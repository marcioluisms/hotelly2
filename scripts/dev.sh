#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

uv sync --all-extras

exec uv run uvicorn hotelly.api.app:app --reload --port 8000
