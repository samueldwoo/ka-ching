"""Sanitized workspace builder for executable frontend tests.

The live-browser suite needs a real local server and enough ledger variation to
exercise dashboard behavior. It must never copy a developer's statements,
history, or rule overlays into the browser-test workspace. This module creates
that workspace from a small, intentionally fictional ledger instead.
"""
import json
import shutil
from pathlib import Path


APP_FILES = ("app.py", "categorize.py", "common.py", "index.html", "parse.py")


def _transaction(
    txn_id,
    date,
    description,
    amount,
    category,
    *,
    source="fixture_card",
    account="Fixture Card",
    **extra,
):
    return {
        "id": txn_id,
        "date": date,
        "description": description,
        "amount": amount,
        "category": category,
        "confidence": "rule",
        "needs_review": False,
        "merchant_key": description.upper(),
        "account": account,
        "source": source,
        "statement": "synthetic-fixture",
        "trip": False,
        "reimbursed": False,
        "trip_auto": False,
        "account_type": "credit_card" if source == "fixture_card" else "checking",
        **extra,
    }


def _ledger():
    """Return seven months of fictional data with the UI states tests require."""
    transactions = []
    for month, dining, groceries, transport in (
        ("2026-01", 88, 62, 21),
        ("2026-02", 96, 58, 18),
        ("2026-03", 84, 71, 26),
        ("2026-04", 110, 64, 22),
        ("2026-05", 92, 69, 29),
        ("2026-06", 105, 61, 24),
        ("2026-07", 77, 54, 20),
    ):
        transactions.extend([
            _transaction(
                f"{month}-pay",
                f"{month}-01",
                "Fixture Payroll",
                2400,
                "Income",
                source="fixture_check",
                account="Fixture Checking",
            ),
            _transaction(
                f"{month}-invest",
                f"{month}-03",
                "Fixture Brokerage Transfer",
                -500,
                "Savings & Investing",
                source="fixture_check",
                account="Fixture Checking",
            ),
            _transaction(
                f"{month}-dining",
                f"{month}-08",
                "Fixture Cafe",
                -dining,
                "Dining",
            ),
            _transaction(
                f"{month}-groceries",
                f"{month}-14",
                "Fixture Market",
                -groceries,
                "Groceries",
            ),
            _transaction(
                f"{month}-transport",
                f"{month}-20",
                "Fixture Transit",
                -transport,
                "Transportation",
            ),
        ])

    # A grouped trip expense demonstrates the independently-collapsed
    # transactions and trip-table group controls.
    transactions.extend([
        _transaction(
            "fixture-coast-flight",
            "2026-05-04",
            "Fixture Coast Rail",
            -210,
            "Travel",
            group_id="fixture-coast-group",
            group_name="Fixture Coast shared costs",
            group_category="Travel",
            agg_date="2026-05-04",
            trip=True,
            trip_id="mtripfixturecoast",
        ),
        _transaction(
            "fixture-coast-hotel",
            "2026-05-05",
            "Fixture Coast Hotel",
            -120,
            "Travel",
            group_id="fixture-coast-group",
            group_name="Fixture Coast shared costs",
            group_category="Travel",
            agg_date="2026-05-04",
            trip=True,
            trip_id="mtripfixturecoast",
        ),
        _transaction(
            "fixture-coast-meal",
            "2026-05-06",
            "Fixture Coast Meal",
            -75,
            "Dining",
            trip=True,
            trip_id="mtripfixturecoast",
            reimbursed=True,
        ),
        _transaction(
            "fixture-mountain-lodge",
            "2026-06-11",
            "Fixture Mountain Lodge",
            -180,
            "Travel",
            trip=True,
            trip_id="mtripfixturemountain",
        ),
        _transaction(
            "fixture-mountain-meal",
            "2026-06-12",
            "Fixture Mountain Meal",
            -60,
            "Dining",
            trip=True,
            trip_id="mtripfixturemountain",
        ),
        _transaction(
            "fixture-staged",
            "2026-07-09",
            "Fixture Airport Shuttle",
            -35,
            "Transportation",
            trip=True,
        ),
    ])
    return transactions


def _raw_ledger():
    """Return parser-shaped rows without UI-only categorization annotations."""
    fields = (
        "id", "date", "description", "amount", "account", "account_type",
        "source", "statement",
    )
    return [{field: transaction[field] for field in fields} for transaction in _ledger()]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def build_frontend_fixture(source_root, destination):
    """Create a runnable, synthetic dashboard workspace at ``destination``."""
    source_root = Path(source_root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    for name in APP_FILES:
        shutil.copy2(source_root / name, destination / name)
    shutil.copytree(source_root / "assets" / "fonts", destination / "assets" / "fonts")

    _write_json(destination / "data" / "categorized.json", _ledger())
    _write_json(destination / "data" / "transactions.json", _raw_ledger())
    _write_json(destination / "data" / "coverage.json", {
        "fixture_check": {
            "account_type": "checking",
            "start": "2026-01-01",
            "end": "2026-07-15",
            "periods": [["2026-01-01", "2026-07-15"]],
        },
        "fixture_card": {
            "account_type": "credit_card",
            "start": "2026-01-01",
            "end": "2026-07-31",
            "periods": [["2026-01-01", "2026-07-31"]],
        },
    })
    _write_json(destination / "data" / "balances.json", {
        "2026-01": 2400,
        "2026-02": 2550,
        "2026-03": 2600,
        "2026-04": 2750,
        "2026-05": 2900,
        "2026-06": 3050,
        "2026-07": 3150,
    })
    _write_json(destination / "data" / "meta.json", {"manual_reconciled": 0})
    _write_json(destination / "data" / "trips.json", [
        {
            "id": "mtripfixturecoast",
            "name": "Fixture Coast Conference",
            "manual": True,
            "start": "2026-05-04",
            "end": "2026-05-06",
            "spend": 405,
            "count": 3,
            "reimbursed": 75,
            "net": 330,
            "by_category": {"Travel": 330, "Dining": 75},
        },
        {
            "id": "mtripfixturemountain",
            "name": "Fixture Mountain Weekend",
            "manual": True,
            "start": "2026-06-11",
            "end": "2026-06-12",
            "spend": 240,
            "count": 2,
            "reimbursed": 0,
            "net": 240,
            "by_category": {"Travel": 180, "Dining": 60},
        },
    ])

    _write_json(destination / "rules" / "budgets.json", {
        "Dining": 125,
        "Groceries": 75,
        "Transportation": 30,
        "Travel": 100,
    })
    _write_json(destination / "rules" / "budget_seeded.json", [
        "Dining", "Groceries", "Transportation", "Travel",
    ])
    _write_json(destination / "rules" / "groups.json", [{
        "id": "fixture-coast-group",
        "name": "Fixture Coast shared costs",
        "category": "Travel",
        "members": ["fixture-coast-flight", "fixture-coast-hotel"],
        "report_month": "2026-05",
    }])
    _write_json(destination / "rules" / "categories.json", {
        "rules": [
            {"category": "Savings & Investing", "match": ["Fixture Brokerage"]},
            {"category": "Income", "match": ["Fixture Payroll"]},
            {"category": "Groceries", "match": ["Fixture Market"]},
            {"category": "Transportation", "match": ["Fixture Transit", "Fixture Airport Shuttle"]},
            {"category": "Travel", "match": [
                "Fixture Coast Rail", "Fixture Coast Hotel", "Fixture Mountain Lodge",
            ]},
            {"category": "Dining", "match": [
                "Fixture Cafe", "Fixture Coast Meal", "Fixture Mountain Meal",
            ]},
        ],
    })
    _write_json(destination / "rules" / "amortize.json", {})
    _write_json(destination / "rules" / "deleted.json", [])
    _write_json(destination / "rules" / "desc_overrides.json", {})
    _write_json(destination / "rules" / "expected_reimb.json", {})
    _write_json(destination / "rules" / "invest.json", {})
    _write_json(destination / "rules" / "manual_trips.json", [
        {"id": "mtripfixturecoast", "name": "Fixture Coast Conference"},
        {"id": "mtripfixturemountain", "name": "Fixture Mountain Weekend"},
    ])
    _write_json(destination / "rules" / "manual_txns.json", [])
    _write_json(destination / "rules" / "overrides.json", {})
    _write_json(destination / "rules" / "reimbursed.json", ["fixture-coast-meal"])
    _write_json(destination / "rules" / "reviewed.json", [])
    _write_json(destination / "rules" / "trip_flags.json", {"fixture-staged": True})
    _write_json(destination / "rules" / "trip_members.json", {
        "fixture-coast-flight": "mtripfixturecoast",
        "fixture-coast-hotel": "mtripfixturecoast",
        "fixture-coast-meal": "mtripfixturecoast",
        "fixture-mountain-lodge": "mtripfixturemountain",
        "fixture-mountain-meal": "mtripfixturemountain",
    })
    _write_json(destination / "rules" / "txn_overrides.json", {})

    # Imports is deliberately empty. Import-editor browser tests supply mocked
    # response data, so a statement fixture is neither needed nor acceptable.
    (destination / "imports").mkdir()
    return destination
