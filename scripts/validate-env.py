#!/usr/bin/env python3
"""Validate and stamp the project-local Python environment."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys


SUPPORTED_MIN = (3, 10)
SUPPORTED_MAX = (3, 15)
MARKER_NAME = ".ka-ching-environment.json"
MARKER_SCHEMA = 2
IMPORT_NAMES = {
    "ofxparse": "ofxparse",
    "pymupdf": "pymupdf",
    "pypdf": "pypdf",
}


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_names(path: Path) -> list[str]:
    names = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if not match:
            raise ValueError(f"Unsupported requirement line in {path.name}: {raw_line}")
        names.append(normalized_name(match.group(1)))
    return names


def constraint_versions(path: Path) -> dict[str, str]:
    versions = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)",
            line,
        )
        if match:
            versions[normalized_name(match.group(1))] = match.group(2)
    return versions


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_state(root: Path) -> dict[str, object]:
    requirements = root / "requirements.txt"
    constraints = root / "constraints.txt"
    if not requirements.is_file() or not constraints.is_file():
        raise ValueError("requirements.txt and constraints.txt must exist")

    names = dependency_names(requirements)
    pins = constraint_versions(constraints)
    missing_pins = [name for name in names if name not in pins]
    if missing_pins:
        raise ValueError(
            "Runtime dependencies need exact constraints: " + ", ".join(missing_pins)
        )

    installed = {}
    for name, expected in pins.items():
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        if version != expected:
            raise ValueError(
                f"{name} is {version}; setup requires the tested version {expected}"
            )
        installed[name] = version

    for name in names:
        if name not in installed:
            raise ValueError(f"Missing runtime dependency: {name}")
        module = IMPORT_NAMES.get(name)
        if module:
            importlib.import_module(module)

    return {
        "schema": MARKER_SCHEMA,
        "project_root": str(root.resolve()),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "files": {
            "constraints.txt": file_hash(constraints),
            "requirements.txt": file_hash(requirements),
        },
        "runtime_dependencies": installed,
    }


def validate_environment(root: Path) -> dict[str, object]:
    if not (SUPPORTED_MIN <= sys.version_info[:2] < SUPPORTED_MAX):
        raise ValueError(
            "Python 3.10 through 3.14 is required; "
            f"found {sys.version_info.major}.{sys.version_info.minor}"
        )
    if sys.prefix == sys.base_prefix:
        raise ValueError("The selected Python is not running inside a virtual environment")

    state = expected_state(root)
    check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode:
        details = (check.stdout + check.stderr).strip()
        raise ValueError(f"pip check failed: {details}")
    return state


def validate_recorded_environment(root: Path, marker: Path) -> dict[str, object]:
    if not (SUPPORTED_MIN <= sys.version_info[:2] < SUPPORTED_MAX):
        raise ValueError("recorded environment uses an unsupported Python")
    if sys.prefix == sys.base_prefix:
        raise ValueError("the selected Python is not running inside a virtual environment")
    if not marker.is_file():
        raise ValueError("environment completion marker is missing")
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(recorded, dict) or recorded.get("schema") != MARKER_SCHEMA:
        raise ValueError("environment completion marker is invalid")
    if recorded.get("project_root") != str(root.resolve()):
        raise ValueError("environment belongs to a different project location")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if recorded.get("python") != python_version:
        raise ValueError("environment Python no longer matches its completion marker")
    dependencies = recorded.get("runtime_dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("environment dependency record is invalid")
    for name, expected in dependencies.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("environment dependency record is invalid")
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"Missing recorded runtime dependency: {name}") from exc
        if installed != expected:
            raise ValueError(
                f"{name} is {installed}; completed environment recorded {expected}"
            )
        module = IMPORT_NAMES.get(name)
        if module:
            importlib.import_module(module)
    check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode:
        details = (check.stdout + check.stderr).strip()
        raise ValueError(f"pip check failed: {details}")
    return recorded


def validate_current_environment(root: Path, marker: Path) -> dict[str, object]:
    recorded = validate_recorded_environment(root, marker)
    requirements = root / "requirements.txt"
    constraints = root / "constraints.txt"
    names = dependency_names(requirements)
    pins = constraint_versions(constraints)
    missing_pins = [name for name in names if name not in pins]
    if missing_pins:
        raise ValueError(
            "Runtime dependencies need exact constraints: " + ", ".join(missing_pins)
        )
    files = {
        "constraints.txt": file_hash(constraints),
        "requirements.txt": file_hash(requirements),
    }
    if recorded.get("files") != files:
        raise ValueError("environment does not match the current dependency files")
    dependencies = recorded["runtime_dependencies"]
    for name in names:
        if dependencies.get(name) != pins[name]:
            raise ValueError(f"environment marker is missing the tested {name} pin")
    return recorded


def write_marker(marker: Path, state: dict[str, object]) -> None:
    temporary = marker.with_name(f"{marker.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the completion marker after successful validation",
    )
    parser.add_argument(
        "--completed",
        action="store_true",
        help="validate the last completed install without requiring current file hashes",
    )
    args = parser.parse_args()
    if args.write and args.completed:
        parser.error("--write and --completed are mutually exclusive")

    root = args.root.resolve()
    marker = Path(sys.prefix) / MARKER_NAME
    try:
        if args.completed:
            state = validate_recorded_environment(root, marker)
        elif args.write:
            state = validate_environment(root)
        else:
            state = validate_current_environment(root, marker)
        if args.write:
            write_marker(marker, state)
    except (ImportError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Environment validation failed: {exc}", file=sys.stderr)
        return 1

    print("Python environment: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
