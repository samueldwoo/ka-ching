"""Public-release tests that use only synthetic examples."""

import json
from pathlib import Path
import subprocess
import sys


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import categorize  # noqa: E402
import parse  # noqa: E402


def test_unmatched_public_outflows_are_neutral_and_reviewable():
    compiled = categorize.build_matcher({"rules": []})
    for txn in (
        {"description": "Unknown wallet payment", "amount": -10.0,
         "source": "custom_wallet", "account_type": "wallet"},
        {"description": "Unknown card purchase", "amount": -10.0,
         "source": "custom_card", "account_type": "credit_card"},
        {"description": "Venmo out: Example", "amount": -10.0,
         "source": "venmo", "account_type": "wallet"},
    ):
        category, confidence, needs_review = categorize.categorize_one(
            txn, compiled, {}
        )
        assert (category, confidence, needs_review) == (
            "Miscellaneous", "low", True,
        )


def test_synthetic_csv_example_matches_and_normalizes(monkeypatch):
    monkeypatch.setattr(parse, "RULES", BASE / "examples" / "rules")
    parse.COVERAGE.clear()
    path = BASE / "examples" / "imports" / "example-checking.csv"
    rows = parse._read_csv_rows(path)
    profiles = parse._generic_profiles()
    profile = profiles["example_checking"]

    assert parse._csv_profile_matches(rows, profile)
    transactions = parse._parse_generic_csv(
        path, "example_checking", profile, rows
    )
    assert [txn["amount"] for txn in transactions] == [2500.0, -64.2, -18.5]
    assert {txn["account"] for txn in transactions} == {"Everyday Checking"}
    assert {txn["account_type"] for txn in transactions} == {"checking"}


def test_example_json_files_are_valid_and_use_known_categories():
    rules = BASE / "examples" / "rules"
    categories = json.loads((rules / "categories.json").read_text(encoding="utf-8"))
    settings = json.loads((rules / "settings.json").read_text(encoding="utf-8"))
    assert all(rule["category"] in categorize.CATEGORIES for rule in categories["rules"])
    assert categorize.configured_fallbacks(settings) == settings["fallback_categories"]
    for name in ("import_profiles.json", "ofx_profiles.json"):
        assert isinstance(json.loads((rules / name).read_text(encoding="utf-8")), dict)


def test_public_documentation_links_and_privacy_guards_exist():
    for name in (
        "README.md", "GETTING_STARTED.md", "PARSER_GUIDE.md", "RULES_GUIDE.md",
    ):
        assert (BASE / name).is_file()
    license_root = BASE if (BASE / "LICENSE").is_file() else BASE / "public"
    for name in ("LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md"):
        assert (license_root / name).is_file()
    ignore_path = BASE / ".gitignore"
    if not (BASE / ".github" / "workflows" / "ci.yml").is_file():
        ignore_path = BASE / "public" / ".gitignore"
    ignore = ignore_path.read_text(encoding="utf-8")
    patterns = (
        "/data/", "/rules/", "/imports/", "*.pdf", "*.csv", "*.ofx", "*.qfx",
    )
    for pattern in patterns:
        assert pattern in ignore


def test_importing_parser_does_not_create_runtime_state(tmp_path):
    for name in ("common.py", "parse.py"):
        (tmp_path / name).write_bytes((BASE / name).read_bytes())
    result = subprocess.run(
        [sys.executable, "-c", "import parse"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "data").exists()


def test_clean_checkout_bootstraps_an_empty_ledger(tmp_path):
    for name in ("app.py", "categorize.py", "common.py", "parse.py", "reconcile.py"):
        (tmp_path / name).write_bytes((BASE / name).read_bytes())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app; app.ensure_data(); app.ensure_budget_seed(); "
                "assert app.load('categorized.json', []) == []; "
                "assert (app.DATA / 'categorized.json').is_file()"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
