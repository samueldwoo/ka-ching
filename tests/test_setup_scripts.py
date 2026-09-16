"""Cross-platform setup and launcher failure-path tests."""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).resolve().parent.parent


def _write_setup_fixture(root):
    (root / "scripts").mkdir(parents=True)
    (root / "app.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "constraints.txt").write_text("# setup fixture\n", encoding="utf-8")
    for name in (
        "setup-unix.sh", "setup-macos.sh", "setup-linux.sh",
        "run-unix.sh", "run-macos.sh", "run-linux.sh",
        "setup-windows.ps1", "run-windows.ps1", "validate-env.py",
    ):
        shutil.copy(BASE / "scripts" / name, root / "scripts" / name)


def _platform_name():
    if os.name == "nt":
        return "windows"
    return "macos" if platform.system() == "Darwin" else "linux"


def _script_command(root, action):
    platform_name = _platform_name()
    script = root / "scripts" / f"{action}-{platform_name}"
    if os.name == "nt":
        return [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(script.with_suffix(".ps1")),
        ]
    return ["bash", str(script.with_suffix(".sh"))]


def _setup_env():
    env = os.environ.copy()
    env.update({
        "KA_CHING_PYTHON": sys.executable,
        "KA_CHING_SKIP_POPPLER": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    })
    return env


def _run(root, action="setup", env=None):
    return subprocess.run(
        _script_command(root, action), cwd=root, env=env or _setup_env(),
        capture_output=True, text=True, timeout=120,
    )


def test_environment_marker_records_installed_transitive_pins(tmp_path, monkeypatch):
    import importlib.util

    script = BASE / "scripts" / "validate-env.py"
    spec = importlib.util.spec_from_file_location("validate_env_test", script)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    (tmp_path / "requirements.txt").write_text("direct>=1\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text(
        "direct==1.0\ntransitive==2.0\n", encoding="utf-8"
    )
    versions = {"direct": "1.0", "transitive": "2.0"}
    monkeypatch.setattr(validator.importlib.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(validator.importlib, "import_module", lambda name: None)

    state = validator.expected_state(tmp_path)

    assert state["runtime_dependencies"] == versions


def test_platform_setup_restores_validated_environment_after_pip_failure(tmp_path):
    root = tmp_path / "project [2026] with spaces"
    _write_setup_fixture(root)

    first = _run(root)
    assert first.returncode == 0, first.stdout + first.stderr
    venv = root / ".venv"
    marker = venv / ".ka-ching-environment.json"
    sentinel = venv / "known-good"
    assert marker.is_file()
    sentinel.touch()

    (root / "requirements.txt").write_text(
        "package-that-does-not-exist==1.0\n", encoding="utf-8"
    )
    (root / "constraints.txt").write_text(
        "package-that-does-not-exist==1.0\n", encoding="utf-8"
    )
    failed = _run(root)

    assert failed.returncode != 0
    assert sentinel.exists(), failed.stdout + failed.stderr
    assert not (root / ".venv.previous").exists()
    assert not (root / ".ka-ching-setup.lock").exists()


def test_platform_setup_rejects_environment_links_without_touching_target(tmp_path):
    root = tmp_path / "linked project"
    _write_setup_fixture(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.touch()
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / ".venv"), str(outside)],
            capture_output=True, text=True,
        )
        assert linked.returncode == 0, linked.stdout + linked.stderr
    else:
        (root / ".venv").symlink_to(outside, target_is_directory=True)

    result = _run(root)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "must not be" in output and ("symbolic link" in output or "junction" in output)
    assert sentinel.exists()


def test_platform_setup_recovers_stale_lock(tmp_path):
    root = tmp_path / "stale lock"
    _write_setup_fixture(root)
    lock = root / ".ka-ching-setup.lock"
    if os.name == "nt":
        lock.write_text("999999\n", encoding="utf-8")
    else:
        lock.mkdir()
        (lock / "pid").write_text("999999\n", encoding="utf-8")
        (lock / "started").write_text("stale\n", encoding="utf-8")

    result = _run(root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not lock.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows uses an OS-held file lock")
def test_unix_setup_refuses_an_active_lock(tmp_path):
    root = tmp_path / "active lock"
    _write_setup_fixture(root)
    lock = root / ".ka-ching-setup.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    started = ""
    (lock / "started").write_text(started, encoding="utf-8")

    result = _run(root)

    assert result.returncode != 0
    assert "Another setup is already running" in result.stderr
    assert lock.is_dir()


def test_environment_marker_detects_dependency_file_drift(tmp_path):
    root = tmp_path / "dependency drift"
    _write_setup_fixture(root)
    setup = _run(root)
    assert setup.returncode == 0, setup.stdout + setup.stderr

    (root / "constraints.txt").write_text(
        "# changed after setup\n", encoding="utf-8"
    )
    run = _run(root, "run")

    assert run.returncode != 0
    output = run.stdout + run.stderr
    assert "does not match" in output or "incomplete or stale" in output


def test_platform_launcher_propagates_application_exit_code(tmp_path):
    root = tmp_path / "launcher exit"
    _write_setup_fixture(root)
    setup = _run(root)
    assert setup.returncode == 0, setup.stdout + setup.stderr

    launched = _run(root, "run")

    assert launched.returncode == 7, launched.stdout + launched.stderr


def test_invalid_existing_environment_is_not_reported_as_restored(tmp_path):
    root = tmp_path / "invalid environment"
    _write_setup_fixture(root)
    venv = root / ".venv"
    venv.mkdir()
    (venv / "partial-install").touch()
    (root / "requirements.txt").write_text(
        "package-that-does-not-exist==1.0\n", encoding="utf-8"
    )
    (root / "constraints.txt").write_text(
        "package-that-does-not-exist==1.0\n", encoding="utf-8"
    )

    result = _run(root)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "restored the previous validated .venv" not in output
    assert not (root / ".venv.previous").exists()
