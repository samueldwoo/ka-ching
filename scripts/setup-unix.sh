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
cd "$ROOT_DIR"

for required in app.py requirements.txt constraints.txt scripts/validate-env.py; do
  if [[ ! -f "$required" ]]; then
    printf 'Setup could not identify the project root at %s (missing %s).\n' \
      "$ROOT_DIR" "$required" >&2
    exit 1
  fi
done

LOCK_DIR="$ROOT_DIR/.ka-ching-setup.lock"
LOCK_HELD=false
ENV_PATHS_READY=false
SETUP_COMPLETE=false
VENV_DIR="$ROOT_DIR/.venv"
BACKUP_DIR="$ROOT_DIR/.venv.previous"

assert_safe_directory() {
  local path="$1"
  if [[ -L "$path" ]]; then
    printf '%s must not be a symbolic link.\n' "$path" >&2
    return 1
  fi
  if [[ -e "$path" && ! -d "$path" ]]; then
    printf '%s exists but is not a directory. Move it aside and rerun setup.\n' \
      "$path" >&2
    return 1
  fi
}

remove_directory() {
  local path="$1"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return
  fi
  assert_safe_directory "$path"
  rm -rf -- "$path"
}

process_started_at() {
  ps -p "$1" -o lstart= 2>/dev/null || true
}

acquire_lock() {
  local attempt existing_pid existing_start current_start unexpected
  if [[ -L "$LOCK_DIR" ]]; then
    printf '%s must not be a symbolic link.\n' "$LOCK_DIR" >&2
    return 1
  fi
  for attempt in 1 2 3; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      process_started_at "$$" > "$LOCK_DIR/started"
      LOCK_HELD=true
      return
    fi
    if [[ ! -d "$LOCK_DIR" ]]; then
      printf '%s exists but is not a directory.\n' "$LOCK_DIR" >&2
      return 1
    fi

    existing_pid="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
    existing_start="$(sed -n '1p' "$LOCK_DIR/started" 2>/dev/null || true)"
    if [[ -z "$existing_pid" ]]; then
      sleep 1
      existing_pid="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
      existing_start="$(sed -n '1p' "$LOCK_DIR/started" 2>/dev/null || true)"
    fi
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] &&
       kill -0 "$existing_pid" 2>/dev/null; then
      current_start="$(process_started_at "$existing_pid")"
      if [[ -z "$existing_start" || "$current_start" == "$existing_start" ]]; then
        printf 'Another setup is already running (process %s).\n' \
          "$existing_pid" >&2
        return 1
      fi
    fi

    unexpected="$(ls -A "$LOCK_DIR" 2>/dev/null | \
      grep -Ev '^(pid|started)$' | sed -n '1p' || true)"
    if [[ -n "$unexpected" ]]; then
      printf 'Stale setup lock contains unexpected files: %s\n' \
        "$LOCK_DIR" >&2
      return 1
    fi
    rm -f -- "$LOCK_DIR/pid" "$LOCK_DIR/started"
    if ! rmdir "$LOCK_DIR" 2>/dev/null; then
      continue
    fi
  done
  printf 'Could not acquire the setup lock. Wait for other setup runs to finish.\n' >&2
  return 1
}

release_lock() {
  local owner
  if [[ "$LOCK_HELD" != true ]]; then
    return
  fi
  owner="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$owner" == "$$" ]]; then
    rm -f -- "$LOCK_DIR/pid" "$LOCK_DIR/started"
    if ! rmdir "$LOCK_DIR" 2>/dev/null; then
      printf 'Warning: setup completed but its lock could not be removed.\n' >&2
    fi
  fi
  LOCK_HELD=false
}

venv_healthy() {
  local path="$1"
  [[ -x "$path/bin/python" ]] &&
    "$path/bin/python" "$ROOT_DIR/scripts/validate-env.py" "$ROOT_DIR" \
      >/dev/null 2>&1
}

venv_completed() {
  local path="$1"
  [[ -x "$path/bin/python" ]] &&
    "$path/bin/python" "$ROOT_DIR/scripts/validate-env.py" --completed "$ROOT_DIR" \
      >/dev/null 2>&1
}

adopt_legacy_venv() {
  local path="$1"
  if [[ -x "$path/bin/python" &&
        ! -f "$path/.ka-ching-environment.json" ]]; then
    "$path/bin/python" "$ROOT_DIR/scripts/validate-env.py" --write "$ROOT_DIR" \
      >/dev/null 2>&1 || true
  fi
}

recover_after_failure() {
  if [[ "$ENV_PATHS_READY" != true || "$SETUP_COMPLETE" == true ]]; then
    return
  fi
  if venv_completed "$VENV_DIR"; then
    :
  elif venv_completed "$BACKUP_DIR"; then
    remove_directory "$VENV_DIR" || true
    if [[ ! -e "$VENV_DIR" ]]; then
      mv "$BACKUP_DIR" "$VENV_DIR" || true
      printf 'Setup failed; restored the previous completed .venv.\n' >&2
    fi
  fi
}

finish() {
  local status=$?
  trap - EXIT
  recover_after_failure
  release_lock
  exit "$status"
}

acquire_lock
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for path in "$VENV_DIR" "$BACKUP_DIR"; do
  assert_safe_directory "$path"
done
ENV_PATHS_READY=true

find_brew() {
  local candidate
  if command -v brew >/dev/null 2>&1; then
    command -v brew
    return
  fi
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

compatible_python() {
  "$1" -c \
    'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)' \
    >/dev/null 2>&1
}

find_path_python() {
  local candidate resolved
  if [[ -n "${KA_CHING_PYTHON:-}" ]]; then
    if compatible_python "$KA_CHING_PYTHON"; then
      printf '%s\n' "$KA_CHING_PYTHON"
      return
    fi
    printf 'KA_CHING_PYTHON is not a supported Python 3.10-3.14 interpreter: %s\n' \
      "$KA_CHING_PYTHON" >&2
    return 1
  fi

  for candidate in python3.13 python3.14 python3.12 python3.11 python3.10 python3; do
    if resolved="$(command -v "$candidate" 2>/dev/null)" &&
       compatible_python "$resolved"; then
      printf '%s\n' "$resolved"
      return
    fi
  done
  return 1
}

find_brew_python() {
  local version formula prefix candidate
  for version in 3.13 3.14 3.12 3.11 3.10; do
    formula="python@$version"
    prefix="$("$BREW_BIN" --prefix "$formula" 2>/dev/null || true)"
    candidate="$prefix/bin/python$version"
    if [[ -n "$prefix" && -x "$candidate" ]] &&
       compatible_python "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

BREW_BIN=""
if [[ "$PLATFORM" == macos ]]; then
  BREW_BIN="$(find_brew || true)"
  if [[ -n "$BREW_BIN" ]]; then
    PATH="$(dirname "$BREW_BIN"):$PATH"
    export PATH
  fi
fi

if [[ -n "${KA_CHING_PYTHON:-}" ]]; then
  PYTHON_BIN="$(find_path_python)"
else
  PYTHON_BIN="$(find_path_python || true)"
  if [[ -z "$PYTHON_BIN" && -n "$BREW_BIN" ]]; then
    PYTHON_BIN="$(find_brew_python || true)"
  fi
fi

PYTHON_FORMULA="python@3.13"
if [[ -z "$PYTHON_BIN" && "$PLATFORM" == macos && -n "$BREW_BIN" ]]; then
  if ! "$BREW_BIN" list --versions "$PYTHON_FORMULA" >/dev/null 2>&1; then
    "$BREW_BIN" install "$PYTHON_FORMULA"
  fi
  PYTHON_BIN="$("$BREW_BIN" --prefix "$PYTHON_FORMULA")/bin/python3.13"
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]] ||
   ! compatible_python "$PYTHON_BIN"; then
  if [[ "$PLATFORM" == macos ]]; then
    printf '%s\n' \
      "Python 3.10-3.14 is required and no compatible interpreter was found." \
      "Install Homebrew from https://brew.sh or Python from https://python.org," \
      "then rerun setup. KA_CHING_PYTHON can select an installed interpreter." >&2
  else
    printf '%s\n' \
      "Python 3.10-3.14 is required and no compatible interpreter was found." \
      "On Ubuntu/Debian, install python3 and python3-venv, then rerun setup." \
      "KA_CHING_PYTHON can select an installed interpreter." >&2
  fi
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import ensurepip, venv' >/dev/null 2>&1; then
  printf '%s\n' \
    "The selected Python cannot create complete virtual environments." \
    "On Ubuntu/Debian, install the matching python3-venv package." >&2
  exit 1
fi

printf 'Using %s at %s\n' "$("$PYTHON_BIN" --version 2>&1)" "$PYTHON_BIN"

if [[ "$PLATFORM" == macos &&
      "${KA_CHING_SKIP_POPPLER:-0}" != 1 ]] &&
   ! command -v pdftotext >/dev/null 2>&1; then
  if [[ -n "$BREW_BIN" ]]; then
    printf 'Installing optional Poppler PDF tools...\n'
    if ! "$BREW_BIN" install poppler; then
      printf '%s\n' \
        "Warning: Poppler could not be installed." \
        "PDF imports will use the local PyMuPDF and pypdf fallbacks." >&2
    fi
  else
    printf '%s\n' \
      "Poppler is not installed; PDF imports will use PyMuPDF and pypdf." \
      "Install Poppler later for the most stable PDF extraction." >&2
  fi
fi

if command -v pdftotext >/dev/null 2>&1; then
  if ! pdftotext -v >/dev/null 2>&1; then
    printf '%s\n' \
      "Warning: pdftotext is present but could not run." \
      "PDF imports will use the local PyMuPDF and pypdf fallbacks." >&2
  fi
elif [[ "$PLATFORM" == linux && "${KA_CHING_SKIP_POPPLER:-0}" != 1 ]]; then
  printf '%s\n' \
    "Poppler is not installed; PDF imports will use PyMuPDF and pypdf." \
    "On Ubuntu/Debian, install poppler-utils for the preferred extractor." >&2
fi

# Resolve any state left between moving the validated environment aside and
# completing its replacement. Legacy environments are adopted only when their
# exact installed versions pass the same validation as a newly built one.
adopt_legacy_venv "$VENV_DIR"
adopt_legacy_venv "$BACKUP_DIR"
if venv_healthy "$VENV_DIR"; then
  remove_directory "$BACKUP_DIR"
elif venv_healthy "$BACKUP_DIR"; then
  remove_directory "$VENV_DIR"
  mv "$BACKUP_DIR" "$VENV_DIR"
  printf 'Recovered the previous validated .venv after an interrupted setup.\n'
elif venv_completed "$VENV_DIR"; then
  remove_directory "$BACKUP_DIR"
elif venv_completed "$BACKUP_DIR"; then
  remove_directory "$VENV_DIR"
  mv "$BACKUP_DIR" "$VENV_DIR"
  printf 'Recovered the previous completed .venv after an interrupted setup.\n'
else
  remove_directory "$VENV_DIR"
  remove_directory "$BACKUP_DIR"
fi

if venv_completed "$VENV_DIR"; then
  mv "$VENV_DIR" "$BACKUP_DIR"
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
VENV_PYTHON="$VENV_DIR/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt -c constraints.txt
"$VENV_PYTHON" "$ROOT_DIR/scripts/validate-env.py" --write "$ROOT_DIR"
SETUP_COMPLETE=true

if ! remove_directory "$BACKUP_DIR"; then
  printf '%s\n' \
    "Setup succeeded, but .venv.previous could not be removed." \
    "The next setup will retry cleanup." >&2
fi

printf '\nSetup complete. Start the app with:\n  ./scripts/run-%s.sh\n\n' \
  "$PLATFORM"
