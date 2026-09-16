#!/usr/bin/env python3
"""
Personal finance dashboard — local server.

One command:  python3 app.py    (then open http://localhost:8000)

The HTTP server uses the standard library. Statement parsing uses the packages
pinned in constraints.txt. The server serves the dashboard (index.html) and a
small JSON API:
  GET  /api/data       -> categorized ledger + categories + budgets + trips + invest plan
  POST /api/*          -> one small ep_* handler per route; see POST_ROUTES (the
                          authoritative list) near the bottom of this file. Covers
                          overrides, budgets, groups, trips, staging, descriptions,
                          amortization, and the budget/invest suggestion engine.

Manual edits persist under rules/ (overlay JSON files) so they survive future
statement re-parses. Each mutating route optionally re-runs categorize.py — see
the recat flag in POST_ROUTES.

On startup it ensures data/categorized.json exists (runs parse+categorize if not).
"""
import errno
import datetime as dt
import hashlib
import gzip
import json
import math
import re
import statistics as st
import subprocess
import sys
import threading
import uuid
from collections import Counter, OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from common import (BASE, DATA, RULES, IMPORTS, CATEGORIES, NON_SPEND, JsonFileError,
                    file_digest, load_json, write_bytes, write_json)

PORT = 8000
MAX_POST_BYTES = 1_000_000

# ThreadingHTTPServer handles each request on its own thread. Rules mutations
# are read-modify-write operations, and refresh/re-categorize replace derived
# data, so a single lock protects the whole POST transaction.
POST_MUTATION_LOCK = threading.RLock()
IMPORT_PREVIEW_CACHE = OrderedDict()
IMPORT_PREVIEW_CACHE_LOCK = threading.Lock()
IMPORT_PREVIEW_INFLIGHT = {}
IMPORT_PREVIEW_CACHE_BYTES = 0
IMPORT_PREVIEW_CACHE_MAX_BYTES = 32 * 1024 * 1024
IMPORT_PREVIEW_CACHE_MAX_FILES = 128
PDF_REVIEW_TOKENS = OrderedDict()
PDF_REVIEW_TOKEN_MAX = 256
IMPORT_PREVIEW_MAX_CANDIDATE_BYTES = 512 * 1024
DATA_RESPONSE_CACHE_LOCK = threading.Lock()
DATA_RESPONSE_CACHE = {"signature": None, "body": None}
DERIVED_ARTIFACTS = (
    "transactions.json", "coverage.json", "balances.json", "categorized.json",
    "trips.json", "meta.json", "catcache.json", "import_report.json",
)


def _rules_snapshot():
    return {p.name: p.read_bytes() for p in RULES.glob("*.json")}


def _restore_rules(snapshot):
    for p in RULES.glob("*.json"):
        if p.name not in snapshot:
            p.unlink()
    for name, content in snapshot.items():
        write_bytes(RULES / name, content)


def _data_snapshot():
    return {
        name: (DATA / name).read_bytes()
        for name in DERIVED_ARTIFACTS
        if (DATA / name).exists()
    }


def _preview_cache_size(result):
    """Return the byte budget consumed by every field retained in the LRU."""
    values = [result.get("text"), result.get("preview"), *result.get("row_candidates", [])]
    return sum(len(value.encode("utf-8", errors="replace"))
               for value in values if isinstance(value, str))


def _bounded_pdf_candidates(candidates, preview):
    """Keep enough complete rows for calibration without caching a huge response."""
    budget = min(
        IMPORT_PREVIEW_MAX_CANDIDATE_BYTES,
        max(0, IMPORT_PREVIEW_CACHE_MAX_BYTES - len(preview.encode("utf-8"))),
    )
    kept = []
    used = 0
    for row in candidates:
        size = len(row.encode("utf-8", errors="replace"))
        if used + size > budget:
            break
        kept.append(row)
        used += size
    return kept


def _pdf_import_preview(path):
    """Extract a PDF once per file revision for the Imports modal."""
    import parse

    global IMPORT_PREVIEW_CACHE_BYTES
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with IMPORT_PREVIEW_CACHE_LOCK:
        cached = IMPORT_PREVIEW_CACHE.get(key)
        if cached is not None:
            IMPORT_PREVIEW_CACHE.move_to_end(key)
    if cached is not None:
        return cached
    # At most one request extracts a particular file revision. Other callers
    # wait for the same text/error result instead of starting another PDF
    # process while the Imports dialog is opened twice.
    with IMPORT_PREVIEW_CACHE_LOCK:
        pending = IMPORT_PREVIEW_INFLIGHT.get(key)
        if pending is None:
            pending = threading.Event()
            IMPORT_PREVIEW_INFLIGHT[key] = pending
            leader = True
        else:
            leader = False
    if not leader:
        pending.wait()
        with IMPORT_PREVIEW_CACHE_LOCK:
            cached = IMPORT_PREVIEW_CACHE.get(key)
        if cached is not None:
            return cached
        # The file changed or disappeared while another request was extracting.
        return _pdf_import_preview(path)
    try:
        text = parse.pdftext(path)
        preview = text[:min(4000, IMPORT_PREVIEW_CACHE_MAX_BYTES)]
        result = {
            "text": text,
            "preview": preview,
            "row_candidates": _bounded_pdf_candidates(
                parse.pdf_transaction_candidates(text), preview
            ),
            "read_error": "",
        }
    except Exception as exc:
        error = str(exc)
        result = {
            "text": None,
            "preview": f"Could not read PDF: {error}",
            "row_candidates": [],
            "read_error": error,
        }
    finally:
        with IMPORT_PREVIEW_CACHE_LOCK:
            # Entries include full extracted text. Keep an LRU cache bounded by
            # both document count and memory rather than retaining 128 large PDFs.
            result_size = _preview_cache_size(result)
            if result_size > IMPORT_PREVIEW_CACHE_MAX_BYTES and result.get("text"):
                # Keep the list view responsive without pinning an arbitrarily
                # large extraction in memory. Selected-file actions re-extract
                # the text on demand through _pdf_import_text().
                result = {
                    **result,
                    "text": None,
                    "preview": result["preview"][:IMPORT_PREVIEW_CACHE_MAX_BYTES],
                    "oversized": True,
                }
                result_size = _preview_cache_size(result)
            IMPORT_PREVIEW_CACHE[key] = result
            IMPORT_PREVIEW_CACHE_BYTES += result_size
            while (len(IMPORT_PREVIEW_CACHE) > 1
                   and (len(IMPORT_PREVIEW_CACHE) > IMPORT_PREVIEW_CACHE_MAX_FILES
                        or IMPORT_PREVIEW_CACHE_BYTES > IMPORT_PREVIEW_CACHE_MAX_BYTES)):
                _, evicted = IMPORT_PREVIEW_CACHE.popitem(last=False)
                IMPORT_PREVIEW_CACHE_BYTES -= _preview_cache_size(evicted)
            IMPORT_PREVIEW_INFLIGHT.pop(key).set()
    return result


def _pdf_import_text(path):
    """Return cached extracted text or raise the same readable extraction error."""
    preview = _pdf_import_preview(path)
    if preview["text"] is None:
        if preview.get("oversized"):
            import parse
            return parse.pdftext(path)
        raise ValueError(preview["read_error"])
    return preview["text"]


def _restore_data(snapshot):
    for name in DERIVED_ARTIFACTS:
        path = DATA / name
        if name in snapshot:
            write_bytes(path, snapshot[name])
        elif path.exists():
            path.unlink()


def run_pipeline():
    """Re-run parse + categorize. Returns (ok, log)."""
    # Keep the visible derived data internally consistent if categorize fails
    # after parse has replaced the raw ledger.
    before = _data_snapshot()
    log = []
    for script in ("parse.py", "categorize.py"):
        r = subprocess.run([sys.executable, str(BASE / script)],
                           capture_output=True, text=True)
        log.append(r.stdout + r.stderr)
        if r.returncode != 0:
            _restore_data(before)
            return False, "\n".join(log)
    return True, "\n".join(log)


def ensure_data():
    if not (DATA / "categorized.json").exists():
        print("No categorized data yet - running pipeline...")
        ok, logtext = run_pipeline()
        if not ok:
            raise RuntimeError(
                "Initial data pipeline failed; refusing to start without a "
                f"categorized ledger.\n{logtext}"
            )


def load(name, default):
    """Read a data/ or rules/ JSON file by name (rules/ files are prefixed)."""
    p = DATA / name if not name.startswith("rules/") else BASE / name
    return load_json(p, default)


_DATA_RESPONSE_FILES = (
    ("categorized.json", []),
    ("rules/budgets.json", {}),
    ("rules/overrides.json", {}),
    ("coverage.json", {}),
    ("rules/groups.json", []),
    ("trips.json", []),
    ("rules/trip_members.json", {}),
    ("rules/invest.json", {}),
    ("balances.json", {}),
    ("meta.json", {}),
)


def _data_response_signature():
    """Identify the exact on-disk revision that backs ``/api/data``."""
    signature = []
    for name, _ in _DATA_RESPONSE_FILES:
        path = DATA / name if not name.startswith("rules/") else BASE / name
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((str(path), None, None))
    return tuple(signature)


def _cached_data_response():
    """Serialize the full dashboard payload once per underlying file revision.

    The browser intentionally receives a coherent full-ledger snapshot today:
    all dashboard tabs derive their totals from the same transactions list.
    Avoiding the repeated disk reads and JSON serialization keeps that contract
    responsive while a future summary/pagination API is designed deliberately.
    """
    signature = _data_response_signature()
    with DATA_RESPONSE_CACHE_LOCK:
        if DATA_RESPONSE_CACHE["signature"] == signature:
            return DATA_RESPONSE_CACHE["body"]
        while True:
            txns = load("categorized.json", [])
            body = {
                "transactions": txns,
                "categories": CATEGORIES,
                "non_spend": list(NON_SPEND),
                "budgets": load("rules/budgets.json", {}),
                "overrides": load("rules/overrides.json", {}),
                "coverage": load("coverage.json", {}),
                "groups": load("rules/groups.json", []),
                "trips": load("trips.json", []),
                "trip_members": load("rules/trip_members.json", {}),
                "invest_plan": load("rules/invest.json", {}),
                "balances": load("balances.json", {}),
                "meta": load("meta.json", {}),
            }
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            # Do not cache a mixture of files when a separate local process
            # changed one during the reads. Rebuild the new revision instead.
            stable_signature = _data_response_signature()
            if stable_signature == signature:
                DATA_RESPONSE_CACHE["signature"] = signature
                DATA_RESPONSE_CACHE["body"] = encoded
                return encoded
            signature = stable_signature


def _import_pdf_path(name):
    """Resolve one selected import without enumerating the rest of the inbox."""
    if not isinstance(name, str) or not name:
        return None
    root = IMPORTS.resolve()
    path = (root / name).resolve()
    if (path.suffix.lower() != ".pdf" or not path.is_file()
            or root not in path.parents):
        return None
    return path


def _import_pdf_detail(path, parse_module, templates, raw_templates, approvals):
    """Inspect one PDF selected in the Imports modal."""
    preview_data = _pdf_import_preview(path)
    source = None
    needs_approval = False
    approved = False
    if not preview_data["read_error"]:
        text = preview_data["text"] or _pdf_import_text(path)
        source = parse_module._detect_pdf_source(text)
        if source is None:
            source = parse_module._template_pdf_source(text, templates)
            needs_approval = source is not None
        if needs_approval:
            template = raw_templates.get(source)
            approved = (
                isinstance(template, dict)
                and parse_module.pdf_import_is_approved(
                    approvals, file_digest(path), source, template)
            )
    return {
        "file": str(path.relative_to(IMPORTS.resolve())),
        "type": "pdf",
        "source": source,
        "preview": preview_data["preview"],
        "row_candidates": preview_data["row_candidates"],
        "read_error": preview_data["read_error"],
        "reference_template": parse_module.reference_pdf_template(source),
        "needs_approval": needs_approval,
        "approved": approved,
    }


def save_rules(name, obj):
    write_json(RULES / name, obj)


# How many trailing complete months feed the budget & invest recommendations.
# One window, one formula everywhere — see _category_budget / suggest_budgets.
BUDGET_WINDOW = 6
# A longer lookback used ONLY to annualize sporadic/annual costs (card fees, car
# registration) that a 6-month window would read as $0 half the year. The budget
# is max(6-mo avg, this-window total / 12) — one rule, no steady/lumpy branch.
ANNUAL_WINDOW = 12


_BUILTIN_ACCOUNT_TYPES = {
    "chase": "checking", "csp": "credit_card", "vx": "credit_card", "venmo": "wallet",
}


def _coverage_for_type(cov, account_type):
    """Return coverage records whose source has the requested account behavior."""
    return [
        record for source, record in cov.items()
        if isinstance(record, dict)
        and record.get("account_type", _BUILTIN_ACCOUNT_TYPES.get(source)) == account_type
    ]


def _partial_month(cov):
    """The newest month whose checking coverage lags tracked spending coverage."""
    # Income is incomplete when *any* checking account lags. Taking the latest
    # end date would incorrectly combine two half-covered accounts into one
    # apparently complete income stream.
    income_end = min([record.get("end", "") for record in _coverage_for_type(cov, "checking")]
                     or [""])
    spend_end = max([
        record.get("end", "") for account_type in ("credit_card", "wallet")
        for record in _coverage_for_type(cov, account_type)
    ] or [""])
    if income_end and spend_end and spend_end > income_end:
        return income_end[:7]
    return None


def _month_bounds(month):
    """Return the first and last ISO date in a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    first = dt.date(year, mon, 1)
    last = dt.date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - dt.timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _income_month_is_complete(cov, month):
    """Whether checking-account coverage spans every day in ``month``."""
    income_coverages = _coverage_for_type(cov, "checking")
    if not income_coverages:
        return True
    first, last = _month_bounds(month)
    for coverage in income_coverages:
        # A checking account cannot make income coverage incomplete before it
        # existed in the dashboard. Once its first imported period begins, it
        # participates in completeness checks from that month onward.
        if coverage.get("start") and coverage["start"] > last:
            continue
        periods = coverage.get("periods", [])
        if not periods:
            start, end = coverage.get("start"), coverage.get("end")
            # Older ledgers only recorded the latest covered date. They cannot
            # establish an earliest boundary, but any calendar month ending on
            # or before that date is complete; the current/end month remains
            # excluded unless it actually ends on the marker.
            complete = (
                bool(end and not start and last <= end)
                or bool(start and end and start <= first and end >= last)
            )
            if not complete:
                return False
            continue

        # A month commonly crosses two statements, so merge contiguous periods
        # for each account independently. Coverage from separate checking
        # accounts must not fill another account's missing statement period.
        cursor = first
        normalized = sorted(
            ((str(p[0]), str(p[1])) for p in periods
             if isinstance(p, (list, tuple)) and len(p) == 2),
            key=lambda p: p[0],
        )
        complete = False
        for start, end in normalized:
            if end < cursor:
                continue
            if start > cursor:
                break
            if end >= last:
                complete = True
                break
            cursor = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
        if not complete:
            return False
    return True


def _complete_income_months(cov, txns):
    """Sorted ledger months with complete checking-income coverage."""
    ledger_months = sorted({
        (t.get("agg_date") or t["date"])[:7]
        for t in txns
    })
    # Preserve the historical behavior for older/manual ledgers without
    # coverage metadata rather than declaring every month unusable.
    if not _coverage_for_type(cov, "checking"):
        return ledger_months
    return [m for m in ledger_months if _income_month_is_complete(cov, m)]


def _monthly_net_spend(txns, partial):
    """
    Per-category, per-month NET spend (charges minus reimbursements), plus the
    per-month grand total. Reimbursements net because they're structural to how
    spending happens here. Returns (cat_month: {cat:{month:net}}, months: sorted,
    totals: {month:net}). Non-spend (income/transfers/savings) excluded.
    """
    cm = defaultdict(lambda: defaultdict(float))
    totals = defaultdict(float)
    allm = set()
    for t in txns:
        m = (t.get("agg_date") or t["date"])[:7]
        cat = t.get("group_category") or t["category"]
        if cat in NON_SPEND:
            continue
        # a trip expense marked reimbursed (paid back by work) cancels out of the
        # spend math — mirror the frontend's isSpend so budgets exclude it too.
        if t.get("reimbursed"):
            continue
        cm[cat][m] += -t["amount"]      # charge -> +, reimbursement -> -
        totals[m] += -t["amount"]
        allm.add(m)
    excluded = {partial} if isinstance(partial, str) else set(partial or ())
    months = sorted(m for m in allm if m not in excluded)
    return cm, months, totals


def _category_budget(monthly_by_cat, months):
    """
    The ONE budgeting rule for a single category, given its {month: net} history
    and the sorted list of complete months. Returns (amount_rounded, basis_str).

        budget = max( trailing-6-month average,
                      trailing-12-month total / 12 )   [rounded to $5]

    The 6-month avg captures current lifestyle; the 12-month annualized term is a
    floor that keeps ANNUAL/sporadic costs (card fees, car registration) from
    vanishing in the 6 months they happen not to appear. For steady categories
    the 6-mo avg dominates, so this collapses to the simple average — still one
    rule, no steady/lumpy branch.
    """
    win6 = months[-BUDGET_WINDOW:]
    win12 = months[-ANNUAL_WINDOW:]
    avg6 = st.mean([max(0.0, monthly_by_cat.get(m, 0.0)) for m in win6]) if win6 else 0.0
    ann = (sum(max(0.0, monthly_by_cat.get(m, 0.0)) for m in win12)
           / ANNUAL_WINDOW) if win12 else 0.0
    base = max(avg6, ann)
    amt = round(base / 5) * 5
    # NOTE: this is a trailing-average BENCHMARK, not a true sinking fund — the
    # saved budget is a flat per-category number with no month-to-month rollover,
    # so a quiet month doesn't literally bank toward a spendy one. The annualized
    # floor just keeps sporadic/annual costs from vanishing between occurrences.
    basis = (f"{len(win6)}-mo average" if avg6 >= ann
             else f"annualized {len(win12)}-mo ÷ 12 (sporadic-cost floor)")
    return float(amt), basis, round(base, 2)


def suggest_budgets():
    """
    ONE budgeting formula (see _category_budget): each category's budget =
    max(trailing-6-mo avg, trailing-12-mo total ÷ 12), rounded to $5. It's a
    trailing-average TARGET/benchmark, not a rolling sinking fund — the saved
    budget is a flat number with no carryover, so a quiet month doesn't literally
    bank toward a spendy one; the 6-mo average just tracks current lifestyle. The
    12-mo annualized term is a floor so annual/sporadic costs (card fees) don't
    vanish in the months they don't appear. No padding — aggressive by design so
    little cash sits idle. Users tweak lines in the UI.
    Returns {budgets, detail, months_used, window}.
    """
    txns = load("categorized.json", [])
    cov = load("coverage.json", {})
    partial = _partial_month(cov)
    complete = set(_complete_income_months(cov, txns))
    ledger_months = {(t.get("agg_date") or t["date"])[:7] for t in txns}
    cm, months, _ = _monthly_net_spend(txns, ledger_months - complete)
    budgets, detail = {}, {}
    for cat, mv in cm.items():
        amt, basis, base = _category_budget(mv, months)
        if amt > 0:
            budgets[cat] = amt
            detail[cat] = {"suggested": amt, "basis": basis, "recent": base}
    return {"budgets": budgets, "detail": detail,
            "months_used": len(months[-BUDGET_WINDOW:]), "window": BUDGET_WINDOW,
            "partial_month": partial,
            "incomplete_months": sorted(ledger_months - complete)}


def _pay_cadence(pay_txns):
    """Checks per year, detected from the median gap between consecutive paydays.
    Biweekly (~14d) -> 26; semi-monthly (~15-16d) -> 24; weekly (~7d) -> 52;
    monthly (~30d) -> 12. Falls back to 26 (biweekly, the common case) when there
    isn't enough history. Replaces the old hardcoded ×2/month assumption."""
    dates = sorted(dt.date.fromisoformat(t["date"]) for t in pay_txns)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if 3 <= (b - a).days <= 45]
    if len(gaps) < 3:
        return 26
    g = st.median(gaps)
    if g <= 10:
        return 52        # weekly (~7d)
    if g <= 14.5:
        return 26        # biweekly (~14d, consistent)
    if g <= 20:
        return 24        # semi-monthly (~15-16d, alternating)
    return 12            # monthly (~30d)


def suggest_invest():
    """
    Dynamic auto-invest recommendation, derived from actual take-home pay and
    NET monthly spending (charges minus reimbursements — reimbursements are
    structural to spending here, so net is the honest basis).

    Auto-invest is COUPLED to the budget so little cash sits idle:

        auto-invest = take-home − budget − buffer

    The three tiers are graduated safety buffers, sized from spending VOLATILITY
    (std dev of monthly total spend) so they stay distinct even when the budget
    already exceeds a typical month:
      * SAFE       buffer = 1·sd     (a full month of swing cushion)
      * AGGRESSIVE buffer = 0.5·sd   (half a swing — lean but real)
      * HARD WALL  buffer = 0        (budget + invest = 100% of pay, no idle cash)
    Take-home = median of recent paychecks (reflects current 401k level). Budget
    couples to the saved per-category budgets (falling back to the formula), so
    the invest math always matches the Budgets tab.
    """
    txns = load("categorized.json", [])
    cov = load("coverage.json", {})
    partial = _partial_month(cov)
    complete = set(_complete_income_months(cov, txns))
    ledger_months = {(t.get("agg_date") or t["date"])[:7] for t in txns}
    cm, months, totals = _monthly_net_spend(txns, ledger_months - complete)
    window = months[-BUDGET_WINDOW:]
    if not window:
        return {"error": "not enough spending history"}

    # Couple to the budget the Budgets tab shows, so "invest = pay − budget −
    # buffer" reconciles with what you see. Use the SAVED amount per category
    # where set, else the computed formula — covering EVERY spend category, even
    # ones not manually budgeted (summing only saved categories would understate
    # the budget and over-recommend investing). Iterate saved ∪ spent so a saved
    # budget for a currently-quiet category isn't dropped. `budget_lines` itemizes
    # each line + its source so the total is auditable against the Budgets tab.
    # Sum EXACTLY the saved budget lines the Budgets table shows — no formula
    # fallback. ensure_budget_seed() guarantees every spend category has a saved
    # line, so there's nothing to fall back for; and a fallback would make a
    # deliberately-zeroed category boomerang back to its formula value (you could
    # never zero a line, and the panel would never match the table). This is what
    # makes the top-line budget and the table total converge and stay dynamic.
    saved = load("rules/budgets.json", {})
    budget_lines = [{"category": c, "amount": float(v), "source": "saved"}
                    for c, v in sorted(saved.items())
                    if c not in NON_SPEND and float(v) > 0]
    budget = sum(l["amount"] for l in budget_lines)

    # Buffer = graduated cash cushion on top of budget, sized from spending
    # volatility. Sample stdev (6 months is a sample, so stdev not pstdev is the
    # honest 1-sigma estimator). The SAFE tier is additionally floored at a
    # minimum reserve (1/4 month of budget) so it can't collapse to ~0 in a quiet
    # stretch (the moment a surprise bill hurts most). Aggressive stays a lean
    # half-sigma and hard_wall stays 0, so the three tiers remain distinct.
    spend_by_mo = [totals[m] for m in window]
    sd = st.stdev(spend_by_mo) if len(spend_by_mo) > 1 else 0.0
    mean_spend = st.mean(spend_by_mo) if spend_by_mo else 0.0
    reserve_floor = round(budget * 0.25)          # SAFE tier keeps at least this much cushion
    safe_buffer = max(sd, reserve_floor)          # comfortable: a full sigma, floored at the reserve
    aggr_buffer = sd * 0.5                         # lean: half a sigma

    # Current take-home per check: median of recent payroll/direct-dep deposits
    # (recent -> reflects current 401k level). Monthly is derived from the ACTUAL
    # pay CADENCE (biweekly = 26/yr, semi-monthly = 24/yr), not a hardcoded ×2 —
    # detected from the median gap between consecutive paydays.
    pay_txns = [t for t in sorted(txns, key=lambda x: x["date"])
                if (t.get("agg_date") or t["date"])[:7] in complete
                and t.get("category") == "Income"
                # Match the ORIGINAL statement text: rules/desc_overrides.json is a
                # display-only relabel (categorize.py applies it last and keeps the
                # original as raw_description), so renaming a deposit row must not
                # silently drop it out of the pay series and rewrite the whole plan.
                and ("payroll" in (t.get("raw_description") or t["description"]).lower()
                     or "direct dep" in (t.get("raw_description") or t["description"]).lower())]
    pays = [t["amount"] for t in pay_txns]
    recent = pays[-6:] if pays else []
    detected_check = st.median(recent) if recent else (st.median(pays) if pays else 0.0)
    checks_per_year = _pay_cadence(pay_txns)      # 26 biweekly / 24 semi-monthly / detected
    # Manual take-home override: a raise takes ~a statement cycle to appear in the
    # ledger, so let the user set current per-check take-home now. Used until the
    # ledger's own recent median catches up to (or passes) it, then auto-defers
    # back to detection so a stale override can't linger. rules/paycheck.json:
    # {"take_home": float, "gross":?, "taxes":?, "retirement":?} (only take_home used).
    ov = load("rules/paycheck.json", {})
    ov_check = float(ov.get("take_home") or 0) or None
    check_overridden = bool(ov_check) and ov_check > detected_check + 1  # still ahead of ledger
    check = ov_check if check_overridden else detected_check
    monthly = check * checks_per_year / 12.0

    def tier(buffer):
        # The PER-CHECK auto-transfer is the source of truth — it's the actual
        # thing you'd set up — rounded to the closest WHOLE DOLLAR (no $25/$50).
        # Everything else is derived FROM it, so the reported monthly is exactly
        # what following the plan invests, and the cushion is the real leftover.
        # This avoids the earlier bug where a rounded per-check but exact monthly
        # disagreed (max showed cushion $0 yet the per-check invested $18 more).
        per_check_target = max(0.0, monthly - budget - buffer) / (checks_per_year / 12.0)
        per_check = round(per_check_target)                # closest whole dollar
        inv_mo = per_check * checks_per_year / 12.0        # what that per-check actually invests/mo
        cushion = monthly - inv_mo - budget                # honest leftover (reflects the real transfer)
        return {
            "buffer": round(buffer),                       # the target slack that sizes this tier
            "cushion_mo": round(cushion),                  # actual cash left in checking after this plan
            "per_check": per_check,                        # whole-dollar auto-transfer amount
            "invest_mo": round(inv_mo),                    # per_check × cadence — the real monthly
            "spendable_mo": round(budget + cushion),
            "annual_base": per_check * checks_per_year,    # per_check × checks/yr — the real annual
        }

    # ACTUAL vs recommended: what you really moved into Savings & Investing each
    # complete month, and the realized savings rate (invested / income). The
    # single decision-relevant number for a heavy investor — the plan is only
    # useful next to what actually happened. Uses gross income (not the paycheck
    # model) so RSU/lump months read honestly.
    inv_by_mo, inc_by_mo = defaultdict(float), defaultdict(float)
    for t in txns:
        m = (t.get("agg_date") or t["date"])[:7]
        # Use the effective category — mirror _monthly_net_spend (and the
        # frontend effective ledger), so a payback reconciled into a spend group
        # (group_category set) nets against that category instead of inflating
        # income. Keying off raw t["category"] here double-counts reconciled
        # Zelle/Venmo 'Income' rows as gross income.
        eff = t.get("group_category") or t["category"]
        if eff == "Savings & Investing" and t["amount"] < 0:
            inv_by_mo[m] += -t["amount"]
        elif eff == "Income":
            inc_by_mo[m] += t["amount"]
    actual_window = [m for m in sorted(set(inv_by_mo) | set(inc_by_mo))
                     if m in complete][-BUDGET_WINDOW:]
    actual = [{"month": m, "invested": round(inv_by_mo[m]),
               "income": round(inc_by_mo[m]),
               "rate": round(inv_by_mo[m] / inc_by_mo[m] * 100) if inc_by_mo[m] else None}
              for m in actual_window]
    avg_invested = round(st.mean([a["invested"] for a in actual])) if actual else 0
    tot_inv = sum(inv_by_mo[m] for m in actual_window)
    tot_inc = sum(inc_by_mo[m] for m in actual_window)
    realized_rate = round(tot_inv / tot_inc * 100) if tot_inc else None

    # WINDFALL stream: income beyond the recurring paycheck (RSU vests, bonuses,
    # lump deposits) that the flat per-check sweep is blind to. A fixed monthly
    # target would either ignore these or wrongly amortize a one-time vest into a
    # standing rate. Instead: recommend investing a high % of any single deposit
    # that lands well above a normal check (this user already invests ~94% of
    # income, so windfalls should mostly flow through). Detect them over the last
    # 12 months so a quarterly/annual vest is caught.
    windfall_rate = 90    # % of a windfall to invest (matches the realized rate)
    recent_cut = [m for m in sorted(inc_by_mo) if m in complete][-12:]
    # A windfall is income landing well above a normal paycheck. Guard on check>0:
    # with no detected paycheck (check==0) the threshold check*1.5 collapses to 0,
    # which would flag EVERY income row as a windfall. No baseline check -> no
    # windfall detection (there's nothing to compare "well above normal" against).
    windfalls = [{"date": t["date"], "amount": round(t["amount"]),
                  "description": t["description"][:40],
                  "suggested_invest": round(t["amount"] * windfall_rate / 100 / 50) * 50}
                 for t in sorted(txns, key=lambda x: x["date"])
                 if check > 0 and t.get("category") == "Income" and t["amount"] > check * 1.5
                 and t["date"][:7] in recent_cut] if check > 0 else []

    return {
        "check": round(check), "monthly_income": round(monthly),
        "check_overridden": check_overridden,          # take-home is a manual raise entry, not ledger-detected
        "detected_check": round(detected_check),        # what the ledger currently shows (for "until it catches up")
        "checks_per_year": checks_per_year,
        "budget_total": round(budget), "budget_lines": budget_lines,
        "spend_stats": {"avg": round(mean_spend), "sd": round(sd)},
        "reserve_floor": reserve_floor,
        "actual": actual, "avg_invested": avg_invested,
        "realized_rate": realized_rate,
        "windfalls": windfalls, "windfall_rate": windfall_rate,
        "months_used": len(window), "window": BUDGET_WINDOW,
        "partial_month": partial,
        "incomplete_months": sorted(ledger_months - complete),
        "tiers": {"safe": tier(safe_buffer),
                  "aggressive": tier(aggr_buffer),
                  "hard_wall": tier(0.0)},
    }


def recategorize():
    """Re-derive data/categorized.json from the parsed ledger + rules overlays.

    In-process (import + call) rather than a subprocess: categorize.main() is
    import-safe (guarded by __main__) and reads/writes files directly, so calling
    it here skips the ~50-100ms interpreter-spawn tax on every mutation while
    preserving identical output. Returns True on success, False if it raised —
    the caller turns a False into a 500 so the UI never claims a stale success."""
    try:
        import contextlib
        import io
        import categorize
        # main() prints a per-run category summary meant for the CLI; swallow it so
        # it doesn't spam the server log on every mutation.
        with contextlib.redirect_stdout(io.StringIO()):
            categorize.main()
        return True
    except Exception as e:
        # keep the failure visible in the server log; caller surfaces a 500
        print(f"recategorize failed: {e!r}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# POST API endpoints
#
# Each endpoint is a small pure-ish function taking the parsed JSON payload and
# returning the response body (a dict/list) for a 200. Validation failures raise
# ApiError(code, message). A route table maps each path to (handler, recat),
# where recat says whether to re-run categorize.py after a successful mutation —
# so that cross-cutting concern lives in ONE place instead of being copy-pasted
# at the end of every branch. Every rules/ mutation persists to disk, so edits
# survive future statement re-parses.
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """Raised by an endpoint handler to return a non-200 response."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _need(cond, code, message):
    if not cond:
        raise ApiError(code, message)


def _pdf_review_signature(path, source, template):
    return {
        "content": file_digest(path),
        "source": source,
        "template": hashlib.sha256(
            json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _issue_pdf_review_token(path, source, template):
    token = uuid.uuid4().hex
    PDF_REVIEW_TOKENS[token] = _pdf_review_signature(path, source, template)
    while len(PDF_REVIEW_TOKENS) > PDF_REVIEW_TOKEN_MAX:
        PDF_REVIEW_TOKENS.popitem(last=False)
    return token


def _consume_pdf_review_token(payload, source, template, path=None):
    token = _string(payload, "review_token")
    expected = (
        _pdf_review_signature(path, source, template)
        if path else {
            "source": source,
            "template": hashlib.sha256(
                json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )
    record = PDF_REVIEW_TOKENS.get(token)
    _need(token and record is not None, 400, "review the complete draft again")
    comparable = record if path else {
        "source": record["source"], "template": record["template"]
    }

    _need(comparable == expected, 400, "draft changed after review; check it again")
    PDF_REVIEW_TOKENS.pop(token, None)


def _string(payload, field, default=""):
    """Return a trimmed string field; reject object/number coercion at the API."""
    value = payload.get(field, default)
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be a string")
    return value.strip()


def _boolean(payload, field, default=False):
    """Return a JSON boolean without treating arbitrary truthy values as true."""
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise ApiError(400, f"{field} must be a boolean")
    return value


def _finite_money(value, field):
    """Return a finite monetary input or raise a client-facing validation error."""
    if isinstance(value, bool):
        raise ApiError(400, f"{field} must be a finite number")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be a finite number")
    if not math.isfinite(amount):
        raise ApiError(400, f"{field} must be a finite number")
    return amount


def _iso_date(value, field="date"):
    """Require the calendar-valid YYYY-MM-DD format used by the ledger."""
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be YYYY-MM-DD")
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ApiError(400, f"{field} must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, f"{field} must be YYYY-MM-DD")
    return value


def _iso_month(value):
    """Require a real YYYY-MM month, not merely a regex-shaped string."""
    if not isinstance(value, str):
        raise ApiError(400, "month must be YYYY-MM")
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ApiError(400, "month must be YYYY-MM")
    try:
        dt.date.fromisoformat(f"{value}-01")
    except ValueError:
        raise ApiError(400, "month must be YYYY-MM")
    return value


def _grouped_member_ids():
    """Return IDs currently participating in a reconciliation group."""
    groups = load("rules/groups.json", [])
    return {
        str(member)
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("members"), list)
        for member in group["members"]
    }


def _need_not_grouped(ids):
    """Reimbursement and reconciliation grouping are mutually exclusive."""
    grouped = set(ids) & _grouped_member_ids()
    _need(not grouped, 400,
          "grouped transactions cannot be marked reimbursed")


def ep_override(payload):
    # {merchant_key, category, id?} -> whole-merchant rule. `id` is the row the
    # user invoked "All <merchant> charges" ON.
    key = _string(payload, "merchant_key").upper()
    cat = _string(payload, "category")
    _need(key and cat in CATEGORIES, 400, "need merchant_key + valid category")
    ov = load("rules/overrides.json", {})
    ov[key] = cat
    save_rules("overrides.json", ov)
    # A single-txn override outranks a merchant rule (categorize.py's txn_overrides
    # check runs first), so a row the user had previously recategorized on its own
    # would keep its old category — i.e. the very row under the cursor is the one
    # row the action visibly skips. Retire that row's override; SIBLING rows keep
    # theirs, since a per-row split there was a deliberate, more specific choice.
    tid = str(payload.get("id", "")).strip()
    if tid:
        tov = load("rules/txn_overrides.json", {})
        if tov.pop(tid, None) is not None:
            save_rules("txn_overrides.json", tov)
    return {"ok": True, "applied": key, "category": cat}


def ep_txn_category(payload):
    # Single-transaction override: keyed by txn id so it doesn't affect other
    # txns from the same merchant.
    txn_id = payload.get("id", "")
    cat = _string(payload, "category")
    _need(txn_id and cat in CATEGORIES, 400, "need id + valid category")
    tov = load("rules/txn_overrides.json", {})
    tov[str(txn_id)] = cat
    save_rules("txn_overrides.json", tov)
    return {"ok": True}


def ep_budget(payload):
    cat = _string(payload, "category")
    amt = payload.get("amount")
    _need(cat in CATEGORIES, 400, "invalid category")
    b = load("rules/budgets.json", {})
    if amt is None or amt == "":
        b.pop(cat, None)
    else:
        amount = _finite_money(amt, "amount")
        # A negative budget is nonsense the two consumers disagree about:
        # suggest_invest() skips it while the Budgets table adds it in, so
        # invest + budget + cushion stops reconciling with the table under it
        # (and the row renders a permanent over-budget bar at a negative %).
        _need(amount >= 0, 400, "budget must not be negative")
        if amount == 0:
            b.pop(cat, None)
        else:
            b[cat] = amount
    save_rules("budgets.json", b)
    return {"ok": True, "budgets": b}


def ep_paycheck(payload):
    # {take_home, gross?, taxes?, retirement?} -> record a manual current-paycheck
    # take-home so a recent raise reflects before it lands in a bank statement. The
    # optional gross/taxes/retirement are stored for reference only. take_home
    # 0/""/None clears the override (revert to ledger detection).
    take_home = payload.get("take_home")
    th = _finite_money(0 if take_home is None or take_home == "" else take_home,
                       "take_home")
    if th <= 0:
        save_rules("paycheck.json", {})
        return {"ok": True, "cleared": True}
    rec = {"take_home": round(th, 2)}
    for k in ("gross", "taxes", "retirement"):
        value = payload.get(k)
        if value not in (None, ""):
            v = _finite_money(value, k)
            if v:
                rec[k] = round(v, 2)
    save_rules("paycheck.json", rec)
    return {"ok": True, "paycheck": rec}


def ep_refresh(payload):
    ok, logtext = run_pipeline()
    return {"ok": ok, "log": logtext}


def ep_pdf_template(payload):
    source = _string(payload, "source")
    template = payload.get("template")
    _need(source and isinstance(template, dict), 400, "need source and template")
    import parse
    # Reject an invalid draft before it can replace a working saved template.
    compiled = parse._compile_pdf_template(source, template)
    profiles = parse._generic_profiles()
    ofx_profiles = parse._ofx_profiles()
    compiled_templates = parse._pdf_templates()
    compiled_templates[source] = compiled
    parse.validate_custom_source_maps(profiles, compiled_templates, ofx_profiles)
    templates = load("rules/pdf_templates.json", {})
    _need(isinstance(templates, dict), 400, "pdf_templates.json must be an object")
    if templates.get(source) != template:
        _consume_pdf_review_token(payload, source, template)
    templates[source] = template
    save_rules("pdf_templates.json", templates)
    return {"ok": True, "source": source}


def ep_pdf_template_preview(payload):
    source = _string(payload, "source")
    template = payload.get("template")
    name = _string(payload, "file")
    compare_to_supported = _boolean(payload, "compare_to_supported")
    _need(source and isinstance(template, dict) and name, 400, "need source, template, and file")
    path = (IMPORTS / name).resolve()
    _need(path.is_file() and IMPORTS.resolve() in path.parents, 400, "file must be inside imports/")
    import parse
    text = _pdf_import_text(path)
    compiled = parse._compile_pdf_template(source, template)
    txns = parse._parse_template_pdf(path, text, source, compiled)
    candidates = parse.pdf_transaction_candidates(text, limit=None)
    def row_matches(row):
        match = compiled["pattern"].match(row)
        return (match is not None and not any(
            pattern.search(match["description"] or "")
            for pattern in compiled["description_excludes"]
        ))

    unmatched = [row for row in candidates if not row_matches(row)]
    result = {"ok": True, "count": len(txns), "transactions": txns,
              "net": round(sum(txn["amount"] for txn in txns), 2),
              "diagnostics": {
                  "candidate_rows": len(candidates),
                  "matched_rows": len(candidates) - len(unmatched),
                  "unmatched_rows": unmatched,
              }}
    result["review_token"] = _issue_pdf_review_token(path, source, template)
    known_source = parse._detect_pdf_source(text)
    if compare_to_supported and known_source is not None:
        expected = parse._adapter_parser(parse.SOURCE_ADAPTERS[known_source])(path, text)

        def key(txn):
            return (txn["date"], re.sub(r"\s+", " ", txn["description"]).upper(),
                    round(float(txn["amount"]), 2))

        actual_keys, expected_keys = Counter(map(key, txns)), Counter(map(key, expected))
        matched = sum((actual_keys & expected_keys).values())
        result["comparison"] = {
            "reference": known_source,
            "expected": len(expected),
            "matched": matched,
            "missing": sum((expected_keys - actual_keys).values()),
            "unexpected": sum((actual_keys - expected_keys).values()),
        }
    return result


def ep_pdf_template_suggest(payload):
    name = _string(payload, "file")
    rows = payload.get("rows")
    adjustments = payload.get("adjustments")
    path = (IMPORTS / name).resolve()
    _need(name and path.is_file() and IMPORTS.resolve() in path.parents, 400,
          "file must be inside imports/")
    import parse
    return {
        "ok": True,
        "suggestion": parse.suggest_pdf_template(_pdf_import_text(path), rows, adjustments),
    }


def ep_pdf_template_delete(payload):
    source = _string(payload, "source")
    templates = load("rules/pdf_templates.json", {})
    _need(source and isinstance(templates, dict) and source in templates, 404,
          "template not found")
    del templates[source]
    save_rules("pdf_templates.json", templates)
    # A deleted template is no longer the parser a user approved. Remove every
    # matching approval so recreating identical rules cannot silently reactivate
    # old files without another review.
    approvals = load("rules/import_approvals.json", {})
    if isinstance(approvals, dict):
        filtered = {
            digest: approval for digest, approval in approvals.items()
            if not (
                approval == source
                or (isinstance(approval, dict) and approval.get("source") == source)
            )
        }
        if len(filtered) != len(approvals):
            save_rules("import_approvals.json", filtered)
    return {"ok": True}


def ep_import_approve(payload):
    name = _string(payload, "file")
    source = _string(payload, "source")
    path = (IMPORTS / name).resolve()
    _need(name and source and path.is_file() and IMPORTS.resolve() in path.parents, 400,
          "file must be inside imports/")
    import parse
    templates = load("rules/pdf_templates.json", {})
    _need(isinstance(templates, dict) and isinstance(templates.get(source), dict), 404,
          "saved PDF template not found")
    # Validate the exact file against the exact rules before minting approval.
    # This is the server-side gate; UI review is helpful but never authoritative.
    compiled = parse._compile_pdf_template(source, templates[source])
    parse._parse_template_pdf(path, _pdf_import_text(path), source, compiled)
    _consume_pdf_review_token(payload, source, templates[source], path)
    digest = file_digest(path)
    approvals = load("rules/import_approvals.json", {})
    _need(isinstance(approvals, dict), 400, "import_approvals.json must be an object")
    approvals[digest] = {
        "source": source,
        "template": parse.pdf_template_signature(templates[source]),
    }
    save_rules("import_approvals.json", approvals)
    return {"ok": True}


def ep_txn_add(payload):
    # {date, description, amount, category, account?} -> new manual txn
    date = _iso_date(payload.get("date"))
    desc = _string(payload, "description")
    cat = _string(payload, "category")
    amt = _finite_money(payload.get("amount"), "amount")
    _need(date and desc and cat in CATEGORIES, 400,
          "need date, description, valid category")
    manual = load("rules/manual_txns.json", [])
    acct = _string(payload, "account", "Manual") or "Manual"
    # Key the id on CONTENT only (date/desc/amt/category/account) — NOT len(manual).
    # A len-based id makes a rapid double-submit compute two DISTINCT ids and append
    # two identical rows that double-count spend. Content-keying makes the create
    # idempotent: an identical second POST resolves to the same id, and we skip the
    # append rather than corrupting every total. (A genuinely intentional duplicate
    # entry is vanishingly rare and can be disambiguated via the description.)
    content = {
        "date": date, "description": desc, "amount": amt,
        "category": cat, "account": acct,
    }
    base_id = "m" + hashlib.sha1(
        f"{date}{desc}{amt}{cat}{acct}".encode()
    ).hexdigest()[:14]
    # An edit preserves its original ID so rules keyed to that ID survive. If
    # the user later recreates the pre-edit content, the base hash can therefore
    # already be occupied by different content. Treat that as a distinct row,
    # while still making an identical retry idempotent.
    same = next((
        t for t in manual
        if all(t.get(key) == value for key, value in content.items())
    ), None)
    if same is not None:
        return {"ok": True, "id": str(same.get("id"))}
    ids = {str(t.get("id")) for t in manual}
    tid = base_id
    suffix = 2
    while tid in ids:
        tid = f"{base_id}-{suffix}"
        suffix += 1
    if tid not in ids:
        manual.append({
            "id": tid, **content,
            "source": "manual",
        })
        save_rules("manual_txns.json", manual)
    return {"ok": True, "id": tid}


def ep_txn_edit(payload):
    # {id, date, description, amount, category, account?} -> edit an existing
    # MANUAL txn in place (same id, so any overrides/groups keyed to it survive).
    # Only manual txns are editable — parsed statement rows are the source of
    # truth and must not be mutated; an unknown/non-manual id is rejected.
    tid = str(payload.get("id", "")).strip()
    date = _iso_date(payload.get("date"))
    desc = _string(payload, "description")
    cat = _string(payload, "category")
    amt = _finite_money(payload.get("amount"), "amount")
    _need(tid and date and desc and cat in CATEGORIES, 400,
          "need id, date, description, valid category")
    manual = load("rules/manual_txns.json", [])
    row = next((m for m in manual if str(m.get("id")) == tid), None)
    _need(row is not None, 404, "not a manual transaction (parsed rows are locked)")
    # An inline relabel (rules/desc_overrides.json) is applied LAST by categorize.py
    # and would keep winning over the new text, so editing the description would
    # return 200 and change nothing visible. Rewriting the row's real description
    # retires the stale display alias; edits that leave the text alone keep it.
    if desc != str(row.get("description", "")):
        dov = load("rules/desc_overrides.json", {})
        if tid in dov:
            dov.pop(tid, None)
            save_rules("desc_overrides.json", dov)
    # Same failure mode, other half: the CATEGORY overlays are also applied after
    # the row's stored category (categorize.apply_manual_layers -> overrides.json
    # by merchant, then txn_overrides.json by id). A manual row ever recategorized
    # with the inline pill could therefore never be recategorized from this dialog
    # again — the save returned 200 and load() repainted the old value. Reconcile
    # the id-keyed overlay so the EFFECTIVE category is the one just saved: keep it
    # (id legitimately outranks a merchant rule) only when a merchant rule would
    # otherwise win, and retire it otherwise.
    from categorize import merchant_key
    tov = load("rules/txn_overrides.json", {})
    merch = load("rules/overrides.json", {}).get(merchant_key(desc))
    if merch and merch != cat:
        changed = tov.get(tid) != cat
        tov[tid] = cat
    else:
        changed = tov.pop(tid, None) is not None
    if changed:
        save_rules("txn_overrides.json", tov)
    row.update({"date": date, "description": desc, "amount": amt, "category": cat,
                "account": _string(payload, "account", "Manual") or "Manual"})
    save_rules("manual_txns.json", manual)
    return {"ok": True, "id": tid}


def ep_txn_delete(payload):
    # {id} -> hide a transaction (manual ones are removed outright)
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    manual = load("rules/manual_txns.json", [])
    # str(.get()) to match how every other manual-txn handler compares ids — a
    # row missing "id", or an int id vs the string tid, must not KeyError/mismatch.
    if any(str(m.get("id")) == tid for m in manual):
        manual = [m for m in manual if str(m.get("id")) != tid]
        save_rules("manual_txns.json", manual)
    else:
        deleted = load("rules/deleted.json", [])
        if tid not in deleted:
            deleted.append(tid)
            save_rules("deleted.json", deleted)
    return {"ok": True}


def ep_txn_restore(payload):
    # {id} -> un-hide a previously deleted (non-manual) transaction
    tid = str(payload.get("id", "")).strip()
    deleted = [d for d in load("rules/deleted.json", []) if d != tid]
    save_rules("deleted.json", deleted)
    return {"ok": True}


def ep_group_save(payload):
    # {id?, name, category, members:[ids]} -> create/update a group
    members_raw = payload.get("members", [])
    _need(isinstance(members_raw, list), 400, "members must be a list")
    members = [str(x) for x in members_raw]
    cat = _string(payload, "category")
    _need(len(members) >= 2 and len(set(members)) == len(members)
          and cat in CATEGORIES, 400,
          "need >=2 members and a valid category")
    reimbursed = set(str(x) for x in load("rules/reimbursed.json", []))
    _need(not (set(members) & reimbursed), 400,
          "reimbursed transactions cannot be grouped")
    # Mirror of ep_amortize's "grouped transactions cannot be amortized" guard —
    # the two are mutually exclusive in BOTH directions. A grouped member collapses
    # into the group's synthetic effective entry, which carries no amortize field,
    # so the smoothed view would silently stop slicing the charge while the row kept
    # drawing its "~ Nmo" badge.
    amortized = set(str(k) for k in load("rules/amortize.json", {}))
    _need(not (set(members) & amortized), 400,
          "amortized transactions cannot be grouped")
    groups = load("rules/groups.json", [])
    gid = str(payload.get("id", "")).strip() or \
        "g" + hashlib.sha1((",".join(sorted(members))).encode()).hexdigest()[:12]
    _need(bool(re.fullmatch(r"g[a-f0-9]{12}", gid)), 400, "invalid group id")
    member_set = set(members)
    overlap = [
        str(other.get("id"))
        for other in groups
        if str(other.get("id")) != gid
        and member_set.intersection(str(x) for x in other.get("members", []))
    ]
    _need(not overlap, 400, "a transaction can belong to only one group")
    prev = next((g for g in groups if g.get("id") == gid), None)
    groups = [g for g in groups if g.get("id") != gid]  # replace if editing
    entry = {
        "id": gid, "name": _string(payload, "name", "Group") or "Group",
        "category": cat, "members": members,
    }
    if _boolean(payload, "trip", False):    # mark the whole group as travel
        entry["trip"] = True
    if prev and prev.get("report_month"):   # preserve reporting-month override on edit
        entry["report_month"] = prev["report_month"]
    groups.append(entry)
    save_rules("groups.json", groups)
    return {"ok": True, "id": gid}


def ep_group_delete(payload):
    # {id} -> ungroup
    gid = str(payload.get("id", "")).strip()
    groups = [g for g in load("rules/groups.json", []) if g.get("id") != gid]
    save_rules("groups.json", groups)
    return {"ok": True}


def ep_group_report_month(payload):
    # {id, month:"YYYY-MM" or ""} -> set/clear a group's reporting month.
    # "" reverts to the default (earliest member's month).
    gid = str(payload.get("id", "")).strip()
    month_raw = payload.get("month", "")
    month = _iso_month(month_raw) if month_raw is not None and month_raw != "" else ""
    groups = load("rules/groups.json", [])
    found = False
    for g in groups:
        if g.get("id") == gid:
            if month:
                g["report_month"] = month
            else:
                g.pop("report_month", None)
            found = True
    _need(found, 404, "group not found")
    save_rules("groups.json", groups)
    return {"ok": True}


def ep_txn_description(payload):
    # {id, description} -> relabel a txn's display description. Purely cosmetic:
    # categorize.py keeps matching on the original text, so this never
    # recategorizes. Empty description reverts to original.
    tid = str(payload.get("id", "")).strip()
    desc = _string(payload, "description")
    _need(tid, 400, "need id")
    dov = load("rules/desc_overrides.json", {})
    if desc:
        dov[tid] = desc
    else:
        dov.pop(tid, None)      # revert to original
    save_rules("desc_overrides.json", dov)
    return {"ok": True}


def ep_trip_flag(payload):
    # {id, trip:bool} -> override whether a txn counts as on-a-trip
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    tf = load("rules/trip_flags.json", {})
    tf[tid] = _boolean(payload, "trip")
    save_rules("trip_flags.json", tf)
    return {"ok": True}


def ep_trip_flag_bulk(payload):
    """Apply one trip-flag state to several transactions in one recategorization."""
    raw_ids = payload.get("ids", [])
    _need(isinstance(raw_ids, list), 400, "ids must be a list")
    ids = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
    _need(ids, 400, "need ids")
    trip = _boolean(payload, "trip")
    tf = load("rules/trip_flags.json", {})
    for tid in ids:
        tf[tid] = trip
    save_rules("trip_flags.json", tf)
    return {"ok": True, "count": len(ids)}


def ep_review_accept(payload):
    # {id, accepted:bool} -> accept a low-confidence auto-categorization as-is,
    # clearing its needs-review flag WITHOUT changing the category. Stored as a
    # set of ids in rules/reviewed.json; survives re-parses.
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    ids = set(str(x) for x in load("rules/reviewed.json", []))
    if _boolean(payload, "accepted", True):
        ids.add(tid)
    else:
        ids.discard(tid)
    save_rules("reviewed.json", sorted(ids))
    return {"ok": True}


def ep_reimburse(payload):
    # {id, reimbursed:bool} -> mark a trip expense as reimbursed by work. It then
    # cancels out of Overview spend (stays in the trip's gross bars). Stored as a
    # set of ids in rules/reimbursed.json.
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    reimbursed = _boolean(payload, "reimbursed")
    if reimbursed:
        _need_not_grouped([tid])
    ids = set(str(x) for x in load("rules/reimbursed.json", []))
    if reimbursed:
        ids.add(tid)
    else:
        ids.discard(tid)
    save_rules("reimbursed.json", sorted(ids))
    return {"ok": True}


def ep_reimburse_bulk(payload):
    # {ids:[...], reimbursed:bool} -> mark/clear many expenses at once (the
    # "mark all reimbursed, then un-toggle exceptions" fast path).
    ids_raw = payload.get("ids", [])
    _need(isinstance(ids_raw, list), 400, "ids must be a list")
    ids_in = [str(x) for x in ids_raw]
    _need(ids_in, 400, "need ids")
    reimbursed = _boolean(payload, "reimbursed")
    if reimbursed:
        _need_not_grouped(ids_in)
    ids = set(str(x) for x in load("rules/reimbursed.json", []))
    if reimbursed:
        ids.update(ids_in)
    else:
        ids.difference_update(ids_in)
    save_rules("reimbursed.json", sorted(ids))
    return {"ok": True}


def ep_trip_expected_reimb(payload):
    # {id, amount} -> record the reimbursement actually received for a trip, so the
    # UI can reconcile it against what's marked reimbursed. amount 0/""/None clears.
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    amount = payload.get("amount")
    amt = _finite_money(0 if amount is None or amount == "" else amount, "amount")
    exp = load("rules/expected_reimb.json", {})
    if amt > 0:
        exp[tid] = round(amt, 2)
    else:
        exp.pop(tid, None)
    save_rules("expected_reimb.json", exp)
    return {"ok": True}


MAX_AMORTIZATION_MONTHS = 120


def ep_amortize(payload):
    # {id, months:int} -> spread this charge over N months in the smoothed view.
    # months<=1 clears it. Real amount/date untouched (cash-basis).
    tid = str(payload.get("id", "")).strip()
    _need(tid, 400, "need id")
    raw = payload.get("months", 0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "months must be a whole number")
    _need(math.isfinite(value) and value.is_integer() and value >= 0, 400,
          "months must be a non-negative whole number")
    n = int(value)
    _need(n <= MAX_AMORTIZATION_MONTHS, 400,
          f"months must be at most {MAX_AMORTIZATION_MONTHS}")
    am = load("rules/amortize.json", {})
    if n and n > 1:
        # Only SETTING is blocked for a grouped txn. Clearing has to stay possible
        # regardless, or a row that ended up both grouped and amortized (before
        # ep_group_save gained its mirror guard) would keep a stale "~ Nmo" badge
        # that could only be removed by hand-editing rules/amortize.json.
        groups = load("rules/groups.json", [])
        _need(not any(tid in {str(member) for member in group.get("members", [])}
                      for group in groups), 400,
              "grouped transactions cannot be amortized")
        am[tid] = n
    else:
        am.pop(tid, None)
    save_rules("amortize.json", am)
    return {"ok": True}


def ep_trip_create(payload):
    # {name} -> a new user-defined trip (e.g. "Road trip", "Chicago")
    name = _string(payload, "name")
    _need(name, 400, "need a trip name")
    trips = load("rules/manual_trips.json", [])
    # Seed with a uuid, NOT len(trips): (name, count) is not unique — deleting a
    # trip and recreating it at the same position minted the identical id, so the
    # deleted trip's overlays (expected reimbursement, pinned members) silently
    # reattached to the new one, and two live trips could collide outright.
    tid = "mtrip" + hashlib.sha1((name + uuid.uuid4().hex).encode()).hexdigest()[:12]
    trips.append({"id": tid, "name": name})
    save_rules("manual_trips.json", trips)
    return {"ok": True, "id": tid}


def ep_trip_rename(payload):
    # {id, name} -> rename a manual trip
    tid = str(payload.get("id", "")).strip()
    name = _string(payload, "name")
    _need(tid and name, 400, "need id and name")
    trips = load("rules/manual_trips.json", [])
    found = False
    for t in trips:
        if t.get("id") == tid:
            t["name"] = name
            found = True
    _need(found, 404, "trip not found")
    save_rules("manual_trips.json", trips)
    return {"ok": True}


def ep_trip_delete(payload):
    # {id} -> remove a manual trip (its txns fall back to auto-detection)
    tid = str(payload.get("id", "")).strip()
    trips = [t for t in load("rules/manual_trips.json", []) if t.get("id") != tid]
    save_rules("manual_trips.json", trips)
    tm = load("rules/trip_members.json", {})
    tm = {k: v for k, v in tm.items() if v != tid}
    save_rules("trip_members.json", tm)
    # Purge the trip's own overlays too, or the reimbursement figure outlives the
    # trip: it stays keyed by a dead trip id forever, and reconciles against a
    # later trip that happens to reuse the id.
    exp = load("rules/expected_reimb.json", {})
    if tid in exp:
        exp.pop(tid, None)
        save_rules("expected_reimb.json", exp)
    return {"ok": True}


def ep_trip_assign(payload):
    # {ids:[...], trip_id} -> assign txns to a manual trip; trip_id=""
    # un-assigns (falls back to auto). Also force-flags them as trips.
    raw_ids = payload.get("ids", [])
    _need(isinstance(raw_ids, list), 400, "ids must be a list")
    ids = [str(x) for x in raw_ids]
    trip_id = _string(payload, "trip_id")
    _need(ids, 400, "need ids")
    if trip_id:
        trips = load("rules/manual_trips.json", [])
        _need(any(str(t.get("id")) == trip_id for t in trips if isinstance(t, dict)),
              404, "trip not found")
    tm = load("rules/trip_members.json", {})
    tf = load("rules/trip_flags.json", {})
    for i in ids:
        if trip_id:
            tm[i] = trip_id
            tf[i] = True          # assigning implies it's a trip
        else:
            tm.pop(i, None)       # un-assign; leave auto trip-flag as-is
    save_rules("trip_members.json", tm)
    save_rules("trip_flags.json", tf)
    return {"ok": True}


def ep_suggest_budgets(payload):
    # {apply?:bool} -> the ONE budgeting formula (see _category_budget): each
    # category = max(trailing-6-mo avg, trailing-12-mo total / 12), rounded to $5.
    # apply=True RESETS the saved budget to exactly the formula (replace, not
    # merge) — clearing any stale manual overrides so the page matches the
    # formula 1:1. This backs the "Reset to suggested" button; the budget is
    # also auto-seeded from this on first load (see ensure_budget_seed).
    sug = suggest_budgets()
    if _boolean(payload, "apply", False):
        save_rules("budgets.json", sug["budgets"])
        return {"ok": True, "budgets": sug["budgets"], "detail": sug["detail"]}
    return sug


def ep_suggest_invest(payload):
    # {tier?: "safe"|"aggressive"|"hard_wall"} -> dynamic auto-invest matrix.
    # With a tier, also persists the chosen per-check target to rules/invest.json
    # so the UI can show "your plan" and reserve projection on load.
    sug = suggest_invest()
    tier = payload.get("tier")
    if tier and tier in sug.get("tiers", {}):
        plan = {"tier": tier, "per_check": sug["tiers"][tier]["per_check"],
                "invest_mo": sug["tiers"][tier]["invest_mo"]}
        save_rules("invest.json", plan)
        return {"ok": True, "plan": plan, **sug}
    return sug


# path -> (handler, recategorize_after_success). recat=True re-runs categorize.py
# after a successful mutation (endpoints that only touch budgets — which the
# categorizer doesn't consume — set it False, matching the original behavior).
POST_ROUTES = {
    "/api/override":            (ep_override,            True),
    "/api/txn_category":        (ep_txn_category,        True),
    "/api/budget":              (ep_budget,              False),
    "/api/refresh":             (ep_refresh,             False),
    "/api/pdf_template":        (ep_pdf_template,        False),
    "/api/pdf_template_preview": (ep_pdf_template_preview, False),
    "/api/pdf_template_suggest": (ep_pdf_template_suggest, False),
    "/api/pdf_template_delete": (ep_pdf_template_delete, False),
    "/api/import_approve":     (ep_import_approve,      False),
    "/api/txn_add":             (ep_txn_add,             True),
    "/api/txn_edit":            (ep_txn_edit,            True),
    "/api/txn_delete":          (ep_txn_delete,          True),
    "/api/txn_restore":         (ep_txn_restore,         True),
    "/api/group_save":          (ep_group_save,          True),
    "/api/group_delete":        (ep_group_delete,        True),
    "/api/group_report_month":  (ep_group_report_month,  True),
    "/api/txn_description":     (ep_txn_description,      True),
    "/api/trip_flag":           (ep_trip_flag,           True),
    "/api/trip_flag_bulk":      (ep_trip_flag_bulk,      True),
    "/api/review_accept":       (ep_review_accept,       True),
    "/api/reimburse":           (ep_reimburse,           True),
    "/api/reimburse_bulk":      (ep_reimburse_bulk,      True),
    "/api/trip_expected_reimb": (ep_trip_expected_reimb, True),
    "/api/amortize":            (ep_amortize,            True),
    "/api/trip_create":         (ep_trip_create,         True),
    "/api/trip_rename":         (ep_trip_rename,         True),
    "/api/trip_delete":         (ep_trip_delete,         True),
    "/api/trip_assign":         (ep_trip_assign,         True),
    "/api/suggest_budgets":     (ep_suggest_budgets,     False),
    "/api/suggest_invest":      (ep_suggest_invest,      False),
    "/api/paycheck":            (ep_paycheck,            False),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json", no_store=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        # gzip when the client accepts it and the payload is big enough to matter.
        # The whole ledger ships on every /api/data (grows with the ledger — MBs at
        # 10k+ txns), so this cuts wire size ~5-8x for near-zero CPU. Small responses
        # skip it (compression overhead isn't worth it under ~1 KB).
        enc = None
        if len(data) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            data = gzip.compress(data, compresslevel=6)
            enc = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if no_store:
            self.send_header("Cache-Control", "no-store")
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _trusted_mutation_request(self):
        """Reject browser cross-site writes while retaining local CLI support."""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return False, "POST requests must use application/json"
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"}
                    or parsed.netloc != self.headers.get("Host", "")):
                return False, "cross-origin POST rejected"
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False, "cross-origin POST rejected"
        return True, ""

    def do_GET(self):
        try:
            request = urlsplit(self.path)
            if request.path in ("/", "/index.html"):
                html = (BASE / "index.html").read_text(encoding="utf-8")
                return self._send(200, html, "text/html; charset=utf-8", no_store=True)
            if request.path.startswith("/assets/fonts/"):
                # Fonts are bundled so the interface keeps its typography
                # without a runtime request to a third-party CDN.
                name = Path(request.path).name
                font = BASE / "assets" / "fonts" / name
                if font.suffix != ".ttf" or not font.is_file():
                    return self._send(404, {"error": "not found"})
                return self._send(200, font.read_bytes(), "font/ttf")
            if request.path == "/api/data":
                with POST_MUTATION_LOCK:
                    body = _cached_data_response()
                return self._send(200, body, no_store=True)
            if request.path == "/api/imports":
                import parse
                selected = parse_qs(request.query).get("file", [None])[0]
                raw_templates = load("rules/pdf_templates.json", {})
                if not isinstance(raw_templates, dict):
                    raise JsonFileError("rules/pdf_templates.json must be an object")
                detail_mode = parse_qs(request.query).get("detail", [""])[0] == "1"
                if detail_mode:
                    path = _import_pdf_path(selected)
                    if path is None:
                        return self._send(404, {"error": "PDF import not found"})
                    templates = parse._pdf_templates()
                    approvals = load("rules/import_approvals.json", {})
                    if not isinstance(approvals, dict):
                        raise JsonFileError("rules/import_approvals.json must be an object")
                    detail = _import_pdf_detail(
                        path, parse, templates, raw_templates, approvals
                    )
                    return self._send(
                        200, {"detail": detail, "templates": raw_templates}, no_store=True
                    )

                files = []
                for path in parse._inbox_files(IMPORTS):
                    name = str(path.relative_to(IMPORTS))
                    # Listing a large mixed inbox must be metadata-only. PDF
                    # extraction, layout inference, and candidate-row creation
                    # happen only after the user selects one file in detail mode.
                    files.append({
                        "file": name,
                        "type": path.suffix.lower().lstrip("."),
                        "source": None,
                        "preview": "",
                        "row_candidates": [],
                        "read_error": "",
                        "reference_template": None,
                        "needs_approval": False,
                        "approved": False,
                    })
                return self._send(200, {"files": files, "templates": raw_templates},
                                  no_store=True)
            return self._send(404, {"error": "not found"})
        except JsonFileError as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        trusted, error = self._trusted_mutation_request()
        if not trusted:
            return self._send(403 if "cross-origin" in error else 415, {"error": error})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return self._send(400, {"error": "invalid Content-Length"})
        if length < 0:
            return self._send(400, {"error": "invalid Content-Length"})
        if length > MAX_POST_BYTES:
            return self._send(413, {"error": "request body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send(400, {"error": "bad json"})

        route = POST_ROUTES.get(self.path)
        if route is None:
            return self._send(404, {"error": "not found"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "JSON body must be an object"})
        handler, recat = route
        with POST_MUTATION_LOCK:
            snapshot = _rules_snapshot()
            data_snapshot = _data_snapshot() if recat else None
            try:
                body = handler(payload)
                if recat and not recategorize():
                    raise RuntimeError("Could not re-derive data (recategorize failed)")
            except ApiError as e:
                _restore_rules(snapshot)
                return self._send(e.code, {"error": e.message})
            except (JsonFileError, OSError, RuntimeError) as e:
                _restore_rules(snapshot)
                if data_snapshot is not None:
                    _restore_data(data_snapshot)
                # The mutation persisted to rules/ but re-deriving categorized.json
                # failed (bad rules file, categorize.py crash, corrupt input). Never
                # claim success: the frontend would toast "done", reload the STALE
                # ledger, and the edit would appear to vanish. Surface it so mutate()
                # / the inline r.error checks show the ⚠ instead. Mirrors the
                # "never claim success blindly" discipline on /api/refresh.
                return self._send(500, {"error": str(e) or "Could not save changes"})
            except (AttributeError, TypeError, ValueError):
                _restore_rules(snapshot)
                return self._send(400, {"error": "invalid request"})
            if self.path == "/api/refresh" and not body.get("ok"):
                return self._send(500, body)
        return self._send(200, body)


def ensure_budget_seed():
    """First-run convenience: if no budget has been saved yet, auto-populate it
    from the one formula so a new user sees sensible budgets immediately —
    no button press. Once saved (even after manual edits), never overwritten;
    the user resets on demand via the Budgets tab. Empty {} counts as unseeded.

    Also TOPS UP any spend category that has NEVER been seeded but now has real
    spend (e.g. Miscellaneous appearing later) with its formula value — WITHOUT
    touching existing lines. This keeps the Budgets table and the invest panel on
    the SAME complete set of categories, so their totals converge.

    A `rules/budget_seeded.json` marker records every category we've ever offered.
    That's what distinguishes a genuinely-new category (top it up) from one the
    user DELIBERATELY ZEROED (ep_budget pops it, but it's in the marker, so it
    stays gone — otherwise it would boomerang back to its formula value on every
    restart and silently re-inflate the budget/invest total)."""
    saved = load("rules/budgets.json", {})
    seeded = set(load("rules/budget_seeded.json", []))
    sug = suggest_budgets()
    formula = sug.get("budgets", {})
    if not saved and not seeded:        # true first run — seed the whole formula
        if formula:
            save_rules("budgets.json", formula)
            save_rules("budget_seeded.json", sorted(formula))
        return
    # top up ONLY categories never seeded before (never overwrite edits, and never
    # resurrect a deliberately-zeroed line — it's already recorded in `seeded`)
    added = {c: amt for c, amt in formula.items()
             if c not in saved and c not in seeded and amt > 0}
    if added:
        saved.update(added)
        save_rules("budgets.json", saved)
    # remember every category now known (prior ∪ formula ∪ saved) so a future zero
    # can never be mistaken for "never seeded"
    known = seeded | set(formula) | set(saved)
    if known != seeded:
        save_rules("budget_seeded.json", sorted(known))


def main():
    ensure_data()
    ensure_budget_seed()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"Port {PORT} is already in use on this computer. "
                "Stop the other app process and try again."
            ) from None
        raise
    print(f"\n  Ka-ching finance dashboard: http://localhost:{PORT}\n")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
