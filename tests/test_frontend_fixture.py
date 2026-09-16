"""Tests for the sanitized browser-test workspace builder."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import pytest

from frontend_fixture import build_frontend_fixture


def _free_port():
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.fail(f"fixture integration tests require localhost binding: {exc}")
        return sock.getsockname()[1]


def test_frontend_fixture_has_no_statement_artifacts(tmp_path):
    source_root = Path(__file__).resolve().parent.parent
    app_dir = build_frontend_fixture(source_root, tmp_path / "app")

    forbidden = {".pdf", ".csv", ".ofx", ".qfx"}
    assert not [p for p in app_dir.rglob("*") if p.suffix.lower() in forbidden]
    assert not [app_dir / name for name in ("ChaseBank", "CSP", "VX", "Venmo")
                if (app_dir / name).exists()]

    transactions = json.loads(
        (app_dir / "data" / "categorized.json").read_text(encoding="utf-8")
    )
    raw_transactions = json.loads(
        (app_dir / "data" / "transactions.json").read_text(encoding="utf-8")
    )
    assert len(transactions) >= 30
    assert len(raw_transactions) == len(transactions)
    assert {txn["source"] for txn in transactions} <= {"fixture_check", "fixture_card"}
    assert {txn["source"] for txn in raw_transactions} <= {"fixture_check", "fixture_card"}
    assert all(txn["statement"] == "synthetic-fixture" for txn in transactions)
    assert all(txn["statement"] == "synthetic-fixture" for txn in raw_transactions)
    assert (app_dir / "app.py").is_file()
    assert (app_dir / "categorize.py").is_file()
    assert (app_dir / "common.py").is_file()
    assert (app_dir / "index.html").is_file()
    assert (app_dir / "parse.py").is_file()
    assert (app_dir / "assets" / "fonts" / "fraunces-variable.ttf").is_file()


def _request_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=0.5) as response:
        return json.loads(response.read())


def test_frontend_fixture_supports_imports_and_recategorizing_mutations(tmp_path):
    source_root = Path(__file__).resolve().parent.parent
    app_dir = build_frontend_fixture(source_root, tmp_path / "app")
    port = _free_port()
    app_path = app_dir / "app.py"
    app_path.write_text(
        app_path.read_text(encoding="utf-8").replace("PORT = 8000", f"PORT = {port}"),
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [sys.executable, str(app_path)],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(25):
            try:
                payload = _request_json(f"http://127.0.0.1:{port}/api/data")
                break
            except OSError:
                time.sleep(0.1)
        else:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(f"synthetic fixture server did not start:\n{stdout}\n{stderr}")
        assert len(payload["transactions"]) >= 30
        assert {txn["source"] for txn in payload["transactions"]} <= {
            "fixture_check", "fixture_card",
        }
        imports = _request_json(f"http://127.0.0.1:{port}/api/imports")
        assert imports == {"files": [], "templates": {}}

        ungroup = _request_json(
            f"http://127.0.0.1:{port}/api/group_delete",
            {"id": "fixture-coast-group"},
        )
        assert ungroup == {"ok": True}
        regrouped = _request_json(f"http://127.0.0.1:{port}/api/data")
        assert len(regrouped["transactions"]) == len(payload["transactions"])
        assert not any(txn.get("group_id") for txn in regrouped["transactions"])

        mutation = _request_json(
            f"http://127.0.0.1:{port}/api/txn_category",
            {"id": "2026-06-dining", "category": "Groceries"},
        )
        assert mutation == {"ok": True}
        refreshed = _request_json(f"http://127.0.0.1:{port}/api/data")
        changed = next(
            txn for txn in refreshed["transactions"] if txn["id"] == "2026-06-dining"
        )
        assert changed["category"] == "Groceries"
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
