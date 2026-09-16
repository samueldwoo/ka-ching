#!/usr/bin/env bash
set -euo pipefail

PLATFORM="${1:-}"
case "$PLATFORM" in
  macos|linux) ;;
  *)
    printf 'Usage: %s <macos|linux>\n' "$0" >&2
    exit 2
    ;;
esac

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
SETUP_COMMAND="./scripts/setup-$PLATFORM.sh"

if [[ ! -x "$VENV_PYTHON" ]]; then
  printf 'Missing .venv. Run %s first.\n' "$SETUP_COMMAND" >&2
  exit 1
fi

if ! "$VENV_PYTHON" "$ROOT_DIR/scripts/validate-env.py" "$ROOT_DIR" \
  >/dev/null; then
  printf 'The .venv is incomplete or stale. Run %s again.\n' \
    "$SETUP_COMMAND" >&2
  exit 1
fi

cd "$ROOT_DIR"
exec "$VENV_PYTHON" app.py
