#!/usr/bin/env python3
"""
Statement parser for personal finance dashboard.

Parses four sources into a single normalized transaction ledger:
  - ChaseBank/  Chase checking PDFs   (source of truth for non-card outflows)
  - CSP/        Chase Sapphire Preferred PDFs (card purchases = spending)
  - VX/         Capital One Venture X PDFs    (card purchases = spending)
  - Venmo/      Venmo CSVs                     (P2P; folded into categories)

Spending model (per user):
  * Card purchases (CSP + VX) are the source of truth for spending.
  * Chase checking payments TO those cards are TRANSFERS (excluded from spend).
  * Other checking outflows (Zelle, ATM, direct debits) are kept.
  * Venmo: money you SEND is spend; money you RECEIVE nets against it.

Uses Poppler's `pdftotext` as the primary PDF extractor, with PyMuPDF and
pypdf fallbacks.

Output: data/transactions.json  (list of normalized txn dicts)
Re-running is idempotent: it re-parses every statement each time, so dropping
a new statement PDF/CSV into a source folder or the mixed imports/ inbox and
re-running appends it automatically.
"""

import csv
import datetime as dt
import hashlib
import io
import math
from collections import Counter, defaultdict
import json
import re
import sys
from pathlib import Path

from common import (DOWNLOADS, IMPORTS, DATA, RULES, JsonFileError, file_digest,
                    load_json, pdftext, money, source_folders, write_bytes, write_json)


# Per-source statement coverage: source -> {"start","end","periods":[[s,e],...]}.
# Lets the dashboard warn when one account's coverage (e.g. income from checking)
# lags another's (e.g. card spending) for the latest, still-partial month.
COVERAGE = {}
ACCOUNT_TYPES = {"checking", "credit_card", "wallet"}

# Checking-account ending balance per STATEMENT, keyed by the statement's close
# DATE: {"YYYY-MM-DD": float}. Pulled from the Chase statement's printed "Ending
# Balance" — the ground-truth cash position. Chase statements close mid-month
# (~11th-14th), so this is NOT a calendar-month-end figure; balances.json is
# derived from these by rolling each statement balance to true month-end (see
# _calendar_month_end_balances). Not used in spend/income math.
CHASE_BALANCES = {}


class StatementParseError(ValueError):
    """A source file cannot be parsed reliably enough to replace the ledger."""


class _OfxStructureError(StatementParseError):
    """An OFX/QFX file lacks the standard account wrappers needed by ofxparse."""


def _calendar_month_end_balances(txns, stmt_balances):
    """Convert statement-close balances -> true calendar-month-end balances.

    Chase statements close mid-month, so a balance labeled "June" is really cash
    on ~June 11. Anchor to each statement's printed ending balance (ground truth)
    and roll it forward with the checking-account transactions from the day after
    the statement close to the last day of the month. Because the raw ledger
    reconciles to each statement to the penny (see reconcile.py), rolling forward
    from the prior statement and backward from the next give the SAME month-end
    figure — no drift. Anchoring every month means errors can't accumulate.

    stmt_balances: {"YYYY-MM-DD" close-date: balance}. Returns {"YYYY-MM": bal}.
    """
    chase = sorted((t for t in txns if t.get("source") == "chase"),
                   key=lambda t: t["date"])
    anchors = sorted(stmt_balances.items())        # [(close_date, balance), ...]
    if not anchors:
        return {}

    def net(a, b):                                 # net chase flow over [a, b] inclusive
        return round(sum(t["amount"] for t in chase if a <= t["date"] <= b), 2)

    def month_end(ym):
        y, m = int(ym[:4]), int(ym[5:7])
        return (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)).isoformat()

    # every month spanned by the statement closes gets a calendar-month-end value
    months = sorted({d[:7] for d in stmt_balances})
    out = {}
    for ym in months:
        target = month_end(ym)
        prior = [(d, b) for d, b in anchors if d <= target]
        if prior:                                  # roll FORWARD from the latest prior statement
            d, b = prior[-1]
            after = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
            out[ym] = round(b + net(after, target), 2)
        else:                                      # target precedes all statements: roll BACK from the first
            d, b = anchors[0]
            after = (dt.date.fromisoformat(target) + dt.timedelta(days=1)).isoformat()
            out[ym] = round(b - net(after, d), 2)
    return out


def record_coverage(source: str, start: dt.date, end: dt.date, account_type=None):
    c = COVERAGE.setdefault(source, {"start": None, "end": None, "periods": []})
    if account_type is not None:
        c["account_type"] = account_type
    c["periods"].append([start.isoformat(), end.isoformat()])
    if c["start"] is None or start < dt.date.fromisoformat(c["start"]):
        c["start"] = start.isoformat()
    if c["end"] is None or end > dt.date.fromisoformat(c["end"]):
        c["end"] = end.isoformat()


def _account_type(value, source):
    """Validate an explicit account behavior; preserve untyped legacy configs."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in ACCOUNT_TYPES:
        raise JsonFileError(
            f"{source} account_type must be one of {', '.join(sorted(ACCOUNT_TYPES))}"
        )
    return value

# Only 3-letter abbreviations are needed: the sole consumer (parse_vx) matches
# month tokens with [A-Z][a-z]{2}. Chase/CSP parse full names via strptime %B.
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def assign_year(month: int, day: int, start: dt.date, end: dt.date) -> int:
    """
    Transaction lines carry MM/DD but no year. Pick the year (start.year or
    end.year) whose resulting date falls inside the statement period. Falls
    back to the closest endpoint if neither lands cleanly (rare edge dates).
    """
    candidates = []
    for yr in {start.year, end.year}:
        try:
            d = dt.date(yr, month, day)
        except ValueError:
            continue  # e.g. 2/29 non-leap
        candidates.append(d)
    if not candidates:
        return end.year
    # Prefer a candidate within [start-3d, end+3d] (posting can trail slightly).
    lo, hi = start - dt.timedelta(days=3), end + dt.timedelta(days=3)
    inside = [d for d in candidates if lo <= d <= hi]
    if inside:
        return inside[0].year
    # Otherwise the candidate closest to the period midpoint.
    mid = start + (end - start) / 2
    return min(candidates, key=lambda d: abs((d - mid).days)).year


# ---------------------------------------------------------------------------
# Chase checking
# ---------------------------------------------------------------------------
def parse_chase_period(text: str):
    m = re.search(
        r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+([A-Z][a-z]+ \d{1,2}, \d{4})",
        text,
    )
    if not m:
        return None
    fmt = "%B %d, %Y"
    return (dt.datetime.strptime(m.group(1), fmt).date(),
            dt.datetime.strptime(m.group(2), fmt).date())


def parse_chase(path: Path, text=None):
    text = pdftext(path) if text is None else text
    period = parse_chase_period(text)
    if not period:
        raise StatementParseError(f"No statement period found in {path.name}")
    start, end = period
    record_coverage("chase", start, end, "checking")
    # Capture the printed ending balance (ground-truth cash position) keyed by
    # the statement's end month. Same figure reconcile.py checks against.
    mbal = re.search(r"Ending Balance\s+\$?(-?[\d,]+\.\d{2})", text)
    txns = []
    # Only look inside transaction-detail blocks to avoid summary noise.
    blocks = re.findall(
        r"\*start\*transaction detail(.*?)\*end\*transaction detail",
        text, re.DOTALL,
    )
    if not blocks:
        raise StatementParseError(f"No Chase transaction detail section found in {path.name}")
    line_re = re.compile(
        r"^\s*(\d{2})/(\d{2})\s+(.*?)\s+(-?[\d,]*\.\d{2})\s+(-?[\d,]*\.\d{2})\s*$"
    )
    for block in blocks:
        for line in block.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            mm, dd, desc, amt, _bal = m.groups()
            month, day = int(mm), int(dd)
            year = assign_year(month, day, start, end)
            amount = money(amt)
            # PyMuPDF can detach a withdrawal's minus sign into the preceding
            # description column. Restore the sign and keep it out of the
            # normalized merchant text.
            if re.search(r"\s-\s*$", desc):
                desc = re.sub(r"\s-\s*$", "", desc)
                amount = -abs(amount)
            txns.append({
                "date": dt.date(year, month, day).isoformat(),
                "description": re.sub(r"\s+", " ", desc).strip(),
                "amount": amount,          # sign as printed: - = outflow
                "account": "Chase Checking",
                "source": "chase",
                "statement": path.name,
            })
    mbeg = re.search(r"Beginning Balance\s+\$?(-?[\d,]+\.\d{2})", text)
    if mbeg and mbal:
        beginning, ending = money(mbeg.group(1)), money(mbal.group(1))
        parsed_ending = round(beginning + sum(txn["amount"] for txn in txns), 2)
        if abs(parsed_ending - ending) >= 0.01:
            raise StatementParseError(
                f"{path.name}: parsed ending balance {parsed_ending:.2f} "
                f"does not match printed {ending:.2f}"
            )
    if mbal:
        CHASE_BALANCES[end.isoformat()] = money(mbal.group(1))
    return txns


# ---------------------------------------------------------------------------
# Chase Sapphire Preferred (CSP)
# ---------------------------------------------------------------------------
def parse_csp_period(text: str):
    m = re.search(
        r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        text,
    )
    if not m:
        return None
    fmt = "%m/%d/%y"
    return (dt.datetime.strptime(m.group(1), fmt).date(),
            dt.datetime.strptime(m.group(2), fmt).date())


def parse_csp(path: Path, text=None):
    text = pdftext(path) if text is None else text
    period = parse_csp_period(text)
    if not period:
        raise StatementParseError(f"No statement period found in {path.name}")
    start, end = period
    record_coverage("csp", start, end, "credit_card")
    txns = []
    lines = text.splitlines()
    section = None  # "credits" or "purchase"
    saw_transaction_section = False
    # MM/DD  <desc ...>  <amount>
    line_re = re.compile(r"^\s*(\d{2})/(\d{2})\s{2,}(.*?)\s{2,}(-?[\d,]*\.\d{2})\s*$")
    for line in lines:
        up = line.strip().upper()
        if up.startswith("PAYMENTS AND OTHER CREDITS"):
            section = "credits"
            saw_transaction_section = True
            continue
        if up == "PURCHASE" or up.startswith("PURCHASE "):
            section = "purchase"
            saw_transaction_section = True
            continue
        if up.startswith("PURCHASES") or up.startswith("TOTAL"):
            # 'PURCHASES' (with S) is the interest-summary block, not txns.
            section = None
            continue
        if section is None:
            continue
        m = line_re.match(line)
        if not m:
            continue
        mm, dd, desc, amt = m.groups()
        month, day = int(mm), int(dd)
        year = assign_year(month, day, start, end)
        raw = money(amt)
        # CSP prints purchases positive and credits negative; negating both
        # normalizes to outflow-negative (matches Chase/Venmo convention):
        # purchase 7.76 -> -7.76 spend; credit -359.64 -> +359.64 inflow.
        amount = -raw
        txns.append({
            "date": dt.date(year, month, day).isoformat(),
            "description": re.sub(r"\s+", " ", desc).strip(),
            "amount": amount,
            "account": "Sapphire Preferred",
            "source": "csp",
            "statement": path.name,
        })
    if not saw_transaction_section:
        raise StatementParseError(f"No CSP transaction detail section found in {path.name}")
    _validate_card_summary(
        path, text, txns,
        r"Purchases\s+\+?\$?([\d,]+\.\d{2})",
        r"Payment,?\s*Credits\s+-?\$?([\d,]+\.\d{2})",
        r"Fees Charged\s+\+?\$?([\d,]+\.\d{2})",
    )
    return txns


# ---------------------------------------------------------------------------
# Capital One Venture X (VX)
# ---------------------------------------------------------------------------
def parse_vx_period(text: str):
    m = re.search(
        r"([A-Z][a-z]{2} \d{1,2}, \d{4})\s*-\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*\|",
        text,
    )
    if not m:
        return None
    fmt = "%b %d, %Y"
    return (dt.datetime.strptime(m.group(1), fmt).date(),
            dt.datetime.strptime(m.group(2), fmt).date())


def parse_vx(path: Path, text=None):
    text = pdftext(path) if text is None else text
    period = parse_vx_period(text)
    if not period:
        raise StatementParseError(f"No statement period found in {path.name}")
    start, end = period
    record_coverage("vx", start, end, "credit_card")
    txns = []
    lines = text.splitlines()
    section = None  # "credits" | "purchase" | "fees" | "interest"
    saw_transaction_section = False
    # Trans Date (Mon DD)  Post Date (Mon DD)  Description...  $Amount
    line_re = re.compile(
        r"^\s*([A-Z][a-z]{2})\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.*?)\s+(-?\s?\$[\d,]*\.\d{2})\s*$"
    )
    for line in lines:
        s = line.strip()
        if s.endswith(": Payments, Credits and Adjustments"):
            section = "credits"
            saw_transaction_section = True
            continue
        if s.endswith(": Transactions"):
            section = "purchase"
            saw_transaction_section = True
            continue
        if s == "Fees":
            section = "fees"
            saw_transaction_section = True
            continue
        if s == "Interest Charged":
            section = "interest"
            continue
        if s.startswith("Total") or "Total Transactions for This Period" in s:
            continue
        if section not in ("credits", "purchase", "fees"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        tmon, tday, _pmon, _pday, desc, amt = m.groups()
        month = MONTHS[tmon.lower()]
        day = int(tday)
        year = assign_year(month, day, start, end)
        val = money(amt)
        # VX prints purchases/fees positive, credits with a leading '-'.
        # Normalize to outflow-negative.
        amount = -abs(val) if section in ("purchase", "fees") else abs(val)
        txns.append({
            "date": dt.date(year, month, day).isoformat(),
            "description": re.sub(r"\s+", " ", desc).strip(),
            "amount": amount,
            "account": "Venture X",
            "source": "vx",
            "statement": path.name,
        })
    if not saw_transaction_section:
        raise StatementParseError(f"No VX transaction detail section found in {path.name}")
    _validate_card_summary(
        path, text, txns,
        r"Transactions\s+\+\s*\$([\d,]+\.\d{2})",
        r"Payments\s+-\s*\$([\d,]+\.\d{2})",
        r"Fees Charged\s+\+\s*\$([\d,]+\.\d{2})",
        allow_extra_credits=True,
    )
    return txns


# ---------------------------------------------------------------------------
# Venmo CSV
# ---------------------------------------------------------------------------
ME = "Sam Woo"


def _validate_card_summary(path, text, txns, outflow_pattern, credit_pattern, fee_pattern,
                           allow_extra_credits=False):
    """Fail closed when a changed detail-row layout no longer matches totals."""
    outflow = re.search(outflow_pattern, text)
    credits = re.search(credit_pattern, text)
    fees = re.search(fee_pattern, text)
    parsed_outflow = round(sum(-t["amount"] for t in txns if t["amount"] < 0), 2)
    parsed_credit = round(sum(t["amount"] for t in txns if t["amount"] > 0), 2)
    expected_outflow = (money(outflow.group(1)) if outflow else 0.0) + (
        money(fees.group(1)) if fees else 0.0
    )
    if outflow and abs(expected_outflow - parsed_outflow) > 0.01:
        raise StatementParseError(
            f"Parsed outflows do not match summary in {path.name}: "
            f"{parsed_outflow:.2f} vs {expected_outflow:.2f}"
        )
    credit_mismatch = False
    if credits:
        credit_mismatch = (
            money(credits.group(1)) - parsed_credit > 0.01
            if allow_extra_credits
            else abs(money(credits.group(1)) - parsed_credit) > 0.01
        )
    if credit_mismatch:
        raise StatementParseError(
            f"Parsed credits do not match summary in {path.name}: "
            f"{parsed_credit:.2f} vs {money(credits.group(1)):.2f}"
        )


def parse_amount_str(s: str):
    """'- $5.00' / '+ $18.82' -> float with sign."""
    s = s.strip()
    if not s:
        return None
    neg = s.startswith("-")
    num = re.sub(r"[^\d.]", "", s)
    if not num:
        return None
    val = float(num)
    return -val if neg else val


def _decode_import_text(path):
    """Decode common bank-export encodings consistently on every platform."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Many Windows-authored CSV/OFX exports still use Windows-1252.
        return raw.decode("cp1252", errors="replace")


def _read_csv_rows(path):
    text = _decode_import_text(path)
    with io.StringIO(text, newline="") as f:
        return list(csv.reader(f))


def parse_venmo(path: Path):
    txns = []
    rows = _read_csv_rows(path)
    # Find header row (the one containing 'Datetime').
    header_idx = None
    for i, row in enumerate(rows):
        if "Datetime" in row:
            header_idx = i
            break
    if header_idx is None:
        raise StatementParseError(f"No Venmo CSV header found in {path.name}")
    header = rows[header_idx]
    col = {name: idx for idx, name in enumerate(header)}
    required = {"Datetime", "Amount (total)", "From", "To", "Note"}
    missing = required - set(col)
    if missing:
        raise StatementParseError(
            f"Venmo CSV missing required columns in {path.name}: {', '.join(sorted(missing))}"
        )
    for row_no, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if len(row) < len(header):
            if any(cell.strip() for cell in row):
                raise StatementParseError(f"Malformed Venmo row {row_no} in {path.name}")
            continue
        datetime_s = row[col["Datetime"]].strip()
        if not datetime_s:
            continue  # balance / disclaimer rows
        try:
            when = dt.datetime.fromisoformat(datetime_s)
        except ValueError as exc:
            raise StatementParseError(f"Invalid Venmo date on row {row_no} in {path.name}") from exc
        amt = parse_amount_str(row[col["Amount (total)"]])
        if amt is None:
            raise StatementParseError(f"Invalid Venmo amount on row {row_no} in {path.name}")
        note = row[col["Note"]].strip()
        frm = row[col["From"]].strip()
        to = row[col["To"]].strip()
        vtype = row[col["Type"]].strip() if "Type" in col else ""
        counterparty = to if frm == ME else frm
        # Direction follows the MONEY (the signed amount), not the From/To field.
        # A Venmo "Charge" lists you as From but money comes IN (positive), so
        # keying off From would mislabel charges — use the sign instead.
        direction = "in" if amt > 0 else "out"
        # "Standard Transfer" / "Instant Transfer" move money between your Venmo
        # balance and your bank — not a payment to anyone. Label them plainly so
        # the categorizer can tag them as transfers (they'd otherwise show as a
        # blank-counterparty "Venmo in/out" and double-count vs the bank side).
        is_bank_transfer = "transfer" in vtype.lower()
        if is_bank_transfer:
            desc = (f"Venmo {vtype} "
                    + ("to bank" if amt < 0 else "from bank"))
        else:
            desc = f"Venmo {direction}: {counterparty}" + (f" — {note}" if note else "")
        txns.append({
            "date": when.date().isoformat(),
            "datetime": when.isoformat(),
            "description": desc,
            "note": note,
            "counterparty": counterparty,
            "amount": amt,             # already signed: - = you paid out
            "account": "Venmo",
            "source": "venmo",
            "statement": path.name,
        })
    # Venmo statements are monthly; approximate coverage from txn dates present.
    if txns:
        ds = sorted(t["date"] for t in txns)
        record_coverage("venmo", dt.date.fromisoformat(ds[0]),
                        dt.date.fromisoformat(ds[-1]), "wallet")
    return txns


# ---------------------------------------------------------------------------
def _detect_pdf_source(text):
    """Identify a known PDF layout without relying on its filename or folder."""
    upper = text.upper()
    if "*START*TRANSACTION DETAIL" in upper:
        return "chase"
    if "OPENING/CLOSING DATE" in upper and "SAPPHIRE PREFERRED" in upper:
        return "csp"
    if "CAPITAL ONE" in upper and ": TRANSACTIONS" in upper:
        return "vx"
    return None


def _compile_pdf_template(source, template):
    """Validate and compile one declarative PDF template."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", source) or source in SOURCE_ADAPTERS:
        raise JsonFileError(f"invalid PDF template source: {source!r}")
    if not isinstance(template, dict):
        raise JsonFileError(f"PDF template {source!r} must be an object")
    account = template.get("account")
    fingerprints = template.get("fingerprints")
    pattern = template.get("transaction_pattern")
    if (not isinstance(account, str) or not account.strip()
            or not isinstance(fingerprints, list) or not fingerprints
            or not all(isinstance(item, str) and item.strip() for item in fingerprints)
            or not isinstance(pattern, str) or not pattern.strip()):
        raise JsonFileError(
            f"PDF template {source!r} needs account, fingerprints, and transaction_pattern"
        )
    account_type = _account_type(template.get("account_type"), f"PDF template {source!r}")
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise JsonFileError(f"PDF template {source!r} has invalid transaction_pattern") from exc
    groups = set(compiled.groupindex)
    signed_amount = "amount" in groups
    debit_credit = {"debit", "credit"} <= groups
    if not {"date", "description"} <= groups or signed_amount == debit_credit:
        raise JsonFileError(
            f"PDF template {source!r} pattern needs date and description plus either "
            "an amount group or debit and credit groups"
        )
    positioned_columns = template.get("debit_credit_columns")
    if positioned_columns is not None:
        if (not signed_amount or debit_credit or not isinstance(positioned_columns, dict)
                or set(positioned_columns) != {"debit", "credit"}
                or not all(isinstance(value, int) and value >= 0
                           for value in positioned_columns.values())):
            raise JsonFileError(
                f"PDF template {source!r} has invalid debit_credit_columns"
            )
    sign = template.get("outflow_sign", "negative")
    if sign not in {"negative", "positive"}:
        raise JsonFileError(f"PDF template {source!r} outflow_sign must be negative or positive")
    date_format = template.get("date_format", "%m/%d/%Y")
    if not isinstance(date_format, str) or not date_format:
        raise JsonFileError(f"PDF template {source!r} needs a date_format")
    bounds = {}
    account_fingerprint = template.get("account_fingerprint")
    if account_fingerprint is not None:
        if not isinstance(account_fingerprint, str) or not account_fingerprint:
            raise JsonFileError(f"PDF template {source!r} has invalid account_fingerprint")
        try:
            bounds["account_fingerprint"] = re.compile(account_fingerprint, re.MULTILINE)
        except re.error as exc:
            raise JsonFileError(f"PDF template {source!r} has invalid account_fingerprint") from exc
    for key in ("transaction_start", "transaction_end"):
        value = template.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise JsonFileError(f"PDF template {source!r} has invalid {key}")
            try:
                bounds[key] = re.compile(value, re.MULTILINE)
            except re.error as exc:
                raise JsonFileError(f"PDF template {source!r} has invalid {key}") from exc
    period = template.get("statement_period")
    if period is not None:
        if not isinstance(period, dict) or not isinstance(period.get("pattern"), str):
            raise JsonFileError(f"PDF template {source!r} has invalid statement_period")
        try:
            period_pattern = re.compile(period["pattern"], re.MULTILINE)
        except re.error as exc:
            raise JsonFileError(f"PDF template {source!r} has invalid statement_period") from exc
        if not {"start", "end"} <= set(period_pattern.groupindex):
            raise JsonFileError(f"PDF template {source!r} period needs named start and end groups")
        period_format = period.get("date_format")
        if not isinstance(period_format, str) or not period_format:
            raise JsonFileError(f"PDF template {source!r} period needs date_format")
        bounds["period"] = (period_pattern, period_format)
    excludes = template.get("description_excludes", [])
    if (not isinstance(excludes, list)
            or not all(isinstance(value, str) and value.strip() for value in excludes)):
        raise JsonFileError(f"PDF template {source!r} has invalid description_excludes")
    bounds["description_excludes"] = [
        re.compile(re.escape(value.strip()), re.IGNORECASE) for value in excludes
    ]
    summary = template.get("summary")
    if summary is not None:
        if not isinstance(summary, dict):
            raise JsonFileError(f"PDF template {source!r} has invalid summary")
        compiled_summary = {}
        for kind in ("outflows", "credits"):
            patterns = summary.get(kind, [])
            if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
                raise JsonFileError(f"PDF template {source!r} has invalid {kind} summary")
            compiled_summary[kind] = []
            for value in patterns:
                try:
                    pattern = re.compile(value, re.MULTILINE)
                except re.error as exc:
                    raise JsonFileError(f"PDF template {source!r} has invalid {kind} summary") from exc
                if "amount" not in pattern.groupindex:
                    raise JsonFileError(
                        f"PDF template {source!r} {kind} summary needs a named amount group"
                    )
                compiled_summary[kind].append(pattern)
        for key in ("allow_extra_credits", "allow_missing_credits"):
            value = summary.get(key, False)
            if not isinstance(value, bool):
                raise JsonFileError(f"PDF template {source!r} has invalid {key}")
            compiled_summary[key] = value
        bounds["summary"] = compiled_summary
    return {
        "account": account.strip(), "account_type": account_type,
        "fingerprints": [item.upper() for item in fingerprints],
        "pattern": compiled, "date_format": date_format, "outflow_sign": sign,
        "amount_mode": (
            "debit_credit" if debit_credit else
            "debit_credit_positions" if positioned_columns is not None else "signed"
        ),
        **({"debit_credit_columns": positioned_columns} if positioned_columns is not None else {}),
        **bounds,
    }


def _pdf_templates():
    """Load declarative PDF templates; configuration never executes code."""
    raw = load_json(RULES / "pdf_templates.json", {})
    if not isinstance(raw, dict):
        raise JsonFileError("rules/pdf_templates.json must be an object")
    return {source: _compile_pdf_template(source, template) for source, template in raw.items()}


def import_config_signature(config):
    """Stable signature of the exact declarative import configuration reviewed."""
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pdf_template_signature(template):
    """Backward-compatible name for a PDF template's configuration signature."""
    return import_config_signature(template)


def import_is_approved(approvals, content_digest, source, config):
    """Require the same file bytes, source, and configuration revision."""
    approval = approvals.get(content_digest)
    return (
        isinstance(approval, dict)
        and approval.get("source") == source
        and approval.get("template") == import_config_signature(config)
    )


def pdf_import_is_approved(approvals, content_digest, source, template):
    """Return whether a PDF's reviewed template still matches this import."""
    return import_is_approved(approvals, content_digest, source, template)


def _template_pdf_source(text, templates):
    """Return one matching template source; ambiguous matches stay unsupported."""
    upper = text.upper()
    matches = [source for source, template in templates.items()
               if all(fingerprint in upper for fingerprint in template["fingerprints"])
               and (not template.get("account_fingerprint")
                    or template["account_fingerprint"].search(text))]
    return matches[0] if len(matches) == 1 else None


def _parse_partial_statement_date(value, date_format):
    """Parse a month/day value against a harmless leap year before year recovery."""
    return dt.datetime.strptime(f"2000 {_normalize_month_token(value)}", f"%Y {date_format}")


def _normalize_month_token(value):
    """Normalize the common non-strptime abbreviation ``Sept`` to ``Sep``."""
    return re.sub(r"\bSept\b", "Sep", value, flags=re.IGNORECASE)


def _month_directive(token):
    """Choose strptime's abbreviated or full month directive for one token."""
    return "%b" if token.lower() in {
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
        "sep", "sept", "oct", "nov", "dec",
    } else "%B"


def _template_match_amount(match, template, source, path):
    """Normalize one signed or debit/credit template match."""
    if template["amount_mode"] == "signed":
        return _parse_generic_amount(match["amount"])
    if template["amount_mode"] == "debit_credit_positions":
        amount = _parse_generic_amount(match["amount"])
        column = match.start("amount") - match.start()
        columns = template["debit_credit_columns"]
        return (-abs(amount) if abs(column - columns["debit"]) <= abs(column - columns["credit"])
                else abs(amount))
    debit = _parse_generic_amount(match["debit"])
    credit = _parse_generic_amount(match["credit"])
    if debit and credit:
        raise StatementParseError(
            f"Ambiguous {source} debit and credit values in {path.name}"
        )
    if not debit and not credit:
        return None
    return -abs(debit) if debit else abs(credit)


def _parse_template_pdf(path, text, source, template):
    start = template.get("transaction_start")
    end = template.get("transaction_end")
    if start:
        starts = list(start.finditer(text))
        if not starts:
            raise StatementParseError(f"No {source} transaction section in {path.name}")
        if end:
            sections = []
            for match in starts:
                section = text[match.end():]
                finish = end.search(section)
                if not finish:
                    raise StatementParseError(
                        f"No {source} transaction-section end in {path.name}"
                    )
                sections.append(section[:finish.start()])
        else:
            sections = [text[starts[0].end():]]
    elif end:
        match = end.search(text)
        if not match:
            raise StatementParseError(f"No {source} transaction-section end in {path.name}")
        sections = [text[:match.start()]]
    else:
        sections = [text]
    period = None
    if "period" in template:
        pattern, fmt = template["period"]
        match = pattern.search(text)
        if not match:
            raise StatementParseError(f"No {source} statement period in {path.name}")
        period = (dt.datetime.strptime(_normalize_month_token(match["start"]), fmt).date(),
                  dt.datetime.strptime(_normalize_month_token(match["end"]), fmt).date())
    txns = []
    for section in sections:
        for match in template["pattern"].finditer(section):
            date_s = match["date"].strip()
            try:
                if period is not None and "%Y" not in template["date_format"] and "%y" not in template["date_format"]:
                    partial = _parse_partial_statement_date(date_s, template["date_format"])
                    when = dt.date(assign_year(partial.month, partial.day, *period),
                                   partial.month, partial.day)
                else:
                    if "%Y" not in template["date_format"] and "%y" not in template["date_format"]:
                        raise StatementParseError(
                            f"{source} transaction dates in {path.name} need a statement date range"
                        )
                    when = dt.datetime.strptime(
                        _normalize_month_token(date_s), template["date_format"]
                    ).date()
                amount = _template_match_amount(match, template, source, path)
            except StatementParseError:
                raise
            except ValueError:
                if period is None:
                    raise StatementParseError(f"Invalid {source} transaction in {path.name}")
                try:
                    partial = _parse_partial_statement_date(date_s, template["date_format"])
                    when = dt.date(assign_year(partial.month, partial.day, *period),
                                   partial.month, partial.day)
                    amount = _template_match_amount(match, template, source, path)
                except StatementParseError:
                    raise
                except ValueError as exc:
                    raise StatementParseError(f"Invalid {source} transaction in {path.name}") from exc
            if amount is None:
                continue
            if (template["amount_mode"] == "signed"
                    and template["outflow_sign"] == "positive"):
                amount = -amount
            description = re.sub(r"\s+", " ", match["description"]).strip()
            if not description:
                raise StatementParseError(f"Empty {source} description in {path.name}")
            if any(pattern.search(description) for pattern in template["description_excludes"]):
                continue
            txns.append({
                "date": when.isoformat(), "description": description, "amount": amount,
                "account": template["account"],
                **({"account_type": template["account_type"]}
                   if template["account_type"] is not None else {}),
                "source": source, "statement": path.name,
            })
    if not txns:
        raise StatementParseError(f"No {source} transactions matched in {path.name}")
    if "summary" in template:
        summary = template["summary"]
        for kind in ("outflows", "credits"):
            patterns = summary[kind]
            if not patterns:
                continue
            expected = 0.0
            for pattern in patterns:
                match = pattern.search(text)
                if not match:
                    if kind == "credits" and summary["allow_missing_credits"]:
                        continue
                    raise StatementParseError(f"Missing {source} {kind} summary in {path.name}")
                try:
                    expected += abs(_parse_generic_amount(match["amount"]))
                except ValueError as exc:
                    raise StatementParseError(f"Invalid {source} {kind} summary in {path.name}") from exc
            actual = (sum(-txn["amount"] for txn in txns if txn["amount"] < 0)
                      if kind == "outflows" else sum(txn["amount"] for txn in txns if txn["amount"] > 0))
            mismatch = (expected - actual > 0.01
                        if kind == "credits" and summary["allow_extra_credits"]
                        else abs(expected - actual) > 0.01)
            if mismatch:
                raise StatementParseError(
                    f"{source} {kind} summary mismatch in {path.name}: "
                    f"{actual:.2f} vs {expected:.2f}"
                )
    record_coverage(source, min(dt.date.fromisoformat(t["date"]) for t in txns),
                    max(dt.date.fromisoformat(t["date"]) for t in txns),
                    template["account_type"])
    return txns


_AMOUNT_TOKEN = (
    r"(?:[-+]?\s?\$?(?:[\d,]+\.\d{2}|\.\d{2})(?:\s*(?:CR|DR))?|"
    r"\(\s*\$?(?:[\d,]+\.\d{2}|\.\d{2})\s*\))"
)
_AMOUNT_VALUE = re.compile(_AMOUNT_TOKEN)
_MONTH_TOKEN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
_SLASH_DATE = re.compile(r"^\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)")
_ISO_DATE = re.compile(r"^\s*(\d{4}[-/]\d{2}[-/]\d{2})")
_MONTH_DATE = re.compile(r"^\s*(" + _MONTH_TOKEN + r"\s+\d{1,2}(?:,\s+\d{4})?)")
_DAY_MONTH_DATE = re.compile(r"^\s*(\d{1,2}\s+" + _MONTH_TOKEN + r"(?:\s+\d{4})?)")
_MONTH_DATE_PAIR = re.compile(
    r"^\s*(" + _MONTH_TOKEN + r"\s+\d{1,2})\s+("
    + _MONTH_TOKEN + r"\s+\d{1,2})\b"
)
_PDF_DATE_PREFIX = (
    r"(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    + _MONTH_TOKEN + r"\s+\d{1,2}(?:,\s+\d{4})?|\d{1,2}\s+"
    + _MONTH_TOKEN + r"(?:\s+\d{4})?)"
)
_PDF_TRANSACTION_LINE = re.compile(
    r"^\s*" + _PDF_DATE_PREFIX
    + r"(?:[ \t]{2,}|\s*\n\s*).+?(?:[ \t]{2,}|\s*\n\s*)"
    + _AMOUNT_TOKEN
    + r"(?:[ \t]+[A-Za-z0-9#][A-Za-z0-9#/:._-]*)*\s*$",
    re.DOTALL,
)
_PDF_DATE_PREFIX_RE = re.compile(r"^\s*" + _PDF_DATE_PREFIX)


def _has_pdf_date_prefix(line):
    return _PDF_DATE_PREFIX_RE.match(line) is not None


def pdf_transaction_candidates(text, limit=250):
    """Return complete or safely joined transaction rows for template setup.

    The import modal deliberately shows a bounded sample. Validation callers use
    ``limit=None`` so their diagnostics cover the whole statement.
    """
    lines = text.splitlines()
    candidates = []
    for index, line in enumerate(lines):
        line = line.rstrip()
        if _PDF_TRANSACTION_LINE.match(line):
            candidates.append(line)
            continue
        # A common PDF layout wraps an overlong merchant name on the next line.
        # Join only a date-leading line with its immediate continuation that
        # forms a complete, amount-terminated row. This avoids guessing across
        # section headers or into the following transaction.
        if not _has_pdf_date_prefix(line) or _AMOUNT_VALUE.search(line):
            continue
        for continuation in lines[index + 1:index + 3]:
            if not continuation.strip() or _has_pdf_date_prefix(continuation):
                break
            candidate = line + "\n" + continuation.rstrip()
            if _PDF_TRANSACTION_LINE.match(candidate):
                candidates.append(candidate)
                break
    return candidates if limit is None else candidates[:limit]


def _candidate_layout(row):
    if _MONTH_DATE_PAIR.match(row):
        return "month_date_pair"
    if _ISO_DATE.match(row):
        return "iso_date"
    if _SLASH_DATE.match(row):
        return "slash_date"
    if _MONTH_DATE.match(row):
        return "month_date"
    if _DAY_MONTH_DATE.match(row):
        return "day_month_date"
    return None


def _infer_statement_period(text, date_format):
    """Return a declarative period extractor for known, text-based date ranges."""
    if date_format in {"%m/%d", "%d/%m"}:
        short_format = "%m/%d" if date_format == "%m/%d" else "%d/%m"
        match = re.search(
            r"Opening/Closing\s+Date\s+(?P<start>\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*"
            r"(?P<end>\d{1,2}/\d{1,2}/\d{2,4})", text, re.I)
        if match:
            digits = len(match["start"].rsplit("/", 1)[1])
            return {
                "pattern": (
                    r"Opening/Closing\s+Date\s+(?P<start>\d{1,2}/\d{1,2}/\d{"
                    f"{digits}" + r"})\s*-\s*(?P<end>\d{1,2}/\d{1,2}/\d{"
                    f"{digits}" + r"})"
                ),
                "date_format": short_format + ("/%Y" if digits == 4 else "/%y"),
            }
        match = re.search(
            r"(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+"
            r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})", text)
        if match and date_format == "%m/%d":
            return {
                "pattern": (
                    r"(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+"
                    r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})"
                ),
                "date_format": "%B %d, %Y",
            }
    if date_format in {"%b %d", "%B %d"}:
        match = re.search(
            r"(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*"
            r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})\s*\|", text)
        if match:
            month_token = match["start"].split()[0]
            return {
                "pattern": (
                    r"(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*"
                    r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})\s*\|"
                ),
                "date_format": _month_directive(month_token) + " %d, %Y",
            }
    if date_format in {"%d %b", "%d %B"}:
        match = re.search(
            r"(?P<start>\d{1,2}\s+[A-Z][a-z]+ \d{4})\s*-\s*"
            r"(?P<end>\d{1,2}\s+[A-Z][a-z]+ \d{4})", text)
        if match:
            month_token = match["start"].split()[1]
            return {
                "pattern": (
                    r"(?P<start>\d{1,2}\s+[A-Z][a-z]+ \d{4})\s*-\s*"
                    r"(?P<end>\d{1,2}\s+[A-Z][a-z]+ \d{4})"
                ),
                "date_format": "%d " + _month_directive(month_token) + " %Y",
            }
    return None


def _guided_phrase(value, label):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > 160:
        raise StatementParseError(f"{label} must be short text")
    return value.strip()


def _guided_phrase_list(values):
    if values is None:
        return []
    if (not isinstance(values, list) or len(values) > 20
            or not all(isinstance(value, str) for value in values)):
        raise StatementParseError("Rows to skip must be a short list of text")
    return [value.strip() for value in values if value.strip()]


def _literal_phrase_pattern(value):
    """Match user-provided statement text without treating it as a regex."""
    return r"\s+".join(re.escape(word) for word in value.split())


def _ordinal(value):
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _looks_like_separate_debit_credit_columns(rows, amount_count):
    """Detect a layout the single-amount template model cannot normalize safely."""
    if amount_count != 2:
        return False
    pairs = [
        tuple(_parse_generic_amount(value) for value in _AMOUNT_VALUE.findall(row))
        for row in rows
        if len(_AMOUNT_VALUE.findall(row)) == 2
    ]
    if len(pairs) < 2 or not all(left >= 0 and right >= 0 for left, right in pairs):
        return False
    exclusive = [(left == 0) != (right == 0) for left, right in pairs]
    return sum(exclusive) >= 2 and sum(exclusive) / len(exclusive) >= 0.6


def _infer_blank_debit_credit_columns(text, rows):
    """Infer blank-cell debit/credit columns from an aligned table header.

    Text extractors often omit blank cells, leaving only one numeric value on
    each row. A Debit/Credit header plus consistent character positions lets us
    preserve direction without guessing from the sign of an unsigned value.
    """
    for header in text.splitlines():
        debit = re.search(r"\bdebits?\b", header, re.I)
        credit = re.search(r"\bcredits?\b", header, re.I)
        if not debit or not credit or debit.start() >= credit.start():
            continue
        columns = {"debit": debit.start(), "credit": credit.start()}
        seen = set()
        for row in rows:
            amounts = list(_AMOUNT_VALUE.finditer(row))
            if len(amounts) != 1:
                continue
            position = amounts[0].start()
            nearest = min(columns, key=lambda key: abs(position - columns[key]))
            # Do not accept a header whose column positions plainly do not
            # align with the transaction values.
            if abs(position - columns[nearest]) <= max(16, (credit.start() - debit.start()) // 2):
                seen.add(nearest)
        if seen == {"debit", "credit"}:
            return columns
    return None


def suggest_pdf_template(text, rows, adjustments=None):
    """Infer a template and expose safe, statement-language adjustment controls."""
    if not isinstance(rows, list) or not rows:
        raise StatementParseError("Select at least one transaction row")
    if not all(isinstance(row, str) for row in rows[:3]):
        raise StatementParseError("Selected transaction rows must be text")
    if adjustments is None:
        adjustments = {}
    if not isinstance(adjustments, dict):
        raise StatementParseError("Draft adjustments must be an object")

    candidates = pdf_transaction_candidates(text)
    layouts = [_candidate_layout(row) for row in candidates]
    layouts = [layout for layout in layouts if layout]
    if not layouts:
        raise StatementParseError("No transaction-like rows were found in this statement")
    selected_layouts = [_candidate_layout(row) for row in rows[:3]]
    selected_layouts = [layout for layout in selected_layouts if layout]
    layout = (selected_layouts[0]
              if selected_layouts and len(set(selected_layouts)) == 1
              else max(dict.fromkeys(layouts), key=layouts.count))
    active_rows = [row for row in candidates if _candidate_layout(row) == layout]
    if not active_rows:
        raise StatementParseError("Could not identify a consistent transaction-row layout")

    date_columns = []
    date_order_choices = []
    requested_date_column = adjustments.get("date_column")
    if layout == "slash_date":
        dates = [_SLASH_DATE.match(row)[1] for row in active_rows]
        parts = [value.split("/") for value in dates]
        widths = {len(value) for value in parts}
        if len(widths) != 1 or widths not in ({2}, {3}):
            raise StatementParseError("The statement uses inconsistent slash-date rows")
        first, second = [int(value[0]) for value in parts], [int(value[1]) for value in parts]
        if any(value > 12 for value in first) and any(value > 12 for value in second):
            raise StatementParseError("The statement has invalid slash-date rows")
        if any(value > 12 for value in first):
            inferred_date_order = "day_first"
        elif any(value > 12 for value in second):
            inferred_date_order = "month_first"
        else:
            inferred_date_order = "month_first"
            date_order_choices = [
                {"id": "month_first", "label": "Month / day (for example, 07/08 is July 8)"},
                {"id": "day_first", "label": "Day / month (for example, 07/08 is 7 August)"},
            ]
        date_order = adjustments.get("date_order", inferred_date_order)
        if date_order not in {"month_first", "day_first"}:
            raise StatementParseError("Choose whether slash dates list month or day first")
        date_format = "%m/%d" if date_order == "month_first" else "%d/%m"
        if len(parts[0]) == 3:
            years = {len(value[2]) for value in parts}
            if years not in ({2}, {4}):
                raise StatementParseError("The statement uses inconsistent slash-date years")
            date_format += "/%y" if years == {2} else "/%Y"
        date_pattern = (
            r"(?P<date>\d{1,2}/\d{1,2}" +
            (r"/\d{2}" if date_format.endswith("%y")
             else r"/\d{4}" if date_format.endswith("%Y") else "") +
            r")"
        )
        date_label = (
            ("month/day" if date_order == "month_first" else "day/month")
            + (" dates with a year" if len(parts[0]) == 3 else " dates")
        )
        suffix = ""
    elif layout == "iso_date":
        dates = [_ISO_DATE.match(row)[1] for row in active_rows]
        slash = all("/" in value for value in dates)
        date_pattern = r"(?P<date>\d{4}/\d{2}/\d{2})" if slash else r"(?P<date>\d{4}-\d{2}-\d{2})"
        date_format = "%Y/%m/%d" if slash else "%Y-%m-%d"
        date_label, suffix = "year-month-day dates", ""
    elif layout == "month_date_pair":
        pair = _MONTH_DATE_PAIR.match(active_rows[0])
        date_columns = [
            {"id": "first", "label": "First date (usually the transaction date)",
             "example": pair[1]},
            {"id": "second", "label": "Second date (usually the posting date)",
             "example": pair[2]},
        ]
        date_column = requested_date_column or "first"
        if date_column not in {"first", "second"}:
            raise StatementParseError("Choose the first or second date column")
        if date_column == "first":
            date_pattern = r"(?P<date>" + _MONTH_TOKEN + r"\s+\d{1,2})"
            suffix = r"\s+" + _MONTH_TOKEN + r"\s+\d{1,2}"
        else:
            date_pattern = (_MONTH_TOKEN + r"\s+\d{1,2}\s+"
                            r"(?P<date>" + _MONTH_TOKEN + r"\s+\d{1,2})")
            suffix = ""
        month_format = _month_directive(pair[1].split()[0])
        date_format, date_label = month_format + " %d", "transaction and posting dates"
    elif layout == "month_date":
        dates = [_MONTH_DATE.match(row)[1] for row in active_rows]
        has_year = all("," in value for value in dates)
        date_pattern = (r"(?P<date>" + _MONTH_TOKEN + r"\s+\d{1,2},\s+\d{4})"
                        if has_year else r"(?P<date>" + _MONTH_TOKEN + r"\s+\d{1,2})")
        month_format = _month_directive(dates[0].split()[0])
        date_format = month_format + (" %d, %Y" if has_year else " %d")
        date_label, suffix = ("month-name dates with a year" if has_year else "month-name dates"), ""
    else:
        dates = [_DAY_MONTH_DATE.match(row)[1] for row in active_rows]
        has_year = all(len(value.split()) == 3 for value in dates)
        date_pattern = (r"(?P<date>\d{1,2}\s+" + _MONTH_TOKEN + r"\s+\d{4})"
                        if has_year else r"(?P<date>\d{1,2}\s+" + _MONTH_TOKEN + r")")
        month_format = _month_directive(dates[0].split()[1])
        date_format = "%d " + month_format + (" %Y" if has_year else "")
        date_label, suffix = (
            "day-first month-name dates with a year" if has_year
            else "day-first month-name dates"
        ), ""

    amount_columns = [len(_AMOUNT_VALUE.findall(row)) for row in active_rows]
    amount_count = max(dict.fromkeys(amount_columns), key=amount_columns.count)
    if amount_count < 1:
        raise StatementParseError("Could not locate an amount column in the selected row layout")
    debit_credit = _looks_like_separate_debit_credit_columns(active_rows, amount_count)
    positioned_debit_credit = (
        None if debit_credit or amount_count != 1
        else _infer_blank_debit_credit_columns(text, active_rows)
    )
    separate_debit_credit = debit_credit or positioned_debit_credit is not None
    default_amount_column = amount_count - 2 if amount_count >= 2 else 0
    requested_amount_column = adjustments.get("amount_column", default_amount_column)
    try:
        amount_column = int(requested_amount_column)
    except (TypeError, ValueError) as exc:
        raise StatementParseError("Choose an amount column") from exc
    if not 0 <= amount_column < amount_count:
        raise StatementParseError("Choose one of the displayed amount columns")
    example_amounts = _AMOUNT_VALUE.findall(active_rows[0])
    amount_choices = []
    if not separate_debit_credit:
        for index in range(amount_count):
            if amount_count == 1:
                label = "Amount"
            elif index == default_amount_column:
                label = f"{_ordinal(index + 1)} money value (recommended)"
            elif index == amount_count - 1:
                label = f"{_ordinal(index + 1)} money value (often the running balance)"
            else:
                label = f"{_ordinal(index + 1)} money value"
            amount_choices.append({"id": str(index), "label": label,
                                   "example": example_amounts[index].strip()})
    sample_amounts = [
        _parse_generic_amount(_AMOUNT_VALUE.findall(row)[amount_column])
        for row in active_rows
        if len(_AMOUNT_VALUE.findall(row)) > amount_column
    ]
    inferred_sign = (
        "negative" if amount_count >= 2 or not any(value > 0 for value in sample_amounts)
        else "positive"
    )
    outflow_sign = adjustments.get("outflow_sign", inferred_sign)
    if outflow_sign not in {"negative", "positive"}:
        raise StatementParseError("Choose how positive amounts should be interpreted")

    wrapped_descriptions = any("\n" in row for row in active_rows)
    column_gap = r"(?:[ \t]{2,}|\s*\n\s*)" if wrapped_descriptions else r"[ \t]{2,}"
    description_pattern = r"(?P<description>[\s\S]*?)" if wrapped_descriptions else r"(?P<description>.*?)"
    trailing_reference = any(
        (matches := list(_AMOUNT_VALUE.finditer(row)))
        and row[matches[-1].end():].strip()
        for row in active_rows
    )
    trailing_suffix = (
        r"(?:[ \t]+[A-Za-z0-9#][A-Za-z0-9#/:._-]*)*\s*$"
        if trailing_reference else r"\s*$"
    )
    if debit_credit:
        pattern = (
            r"^\s*" + date_pattern + suffix + column_gap + description_pattern
            + column_gap + r"(?P<debit>" + _AMOUNT_TOKEN + r")[ \t]+"
            + r"(?P<credit>" + _AMOUNT_TOKEN + r")" + trailing_suffix
        )
    else:
        preceding_amounts = "".join(_AMOUNT_TOKEN + r"[ \t]+" for _ in range(amount_column))
        trailing_amounts = "".join(
            r"[ \t]+" + _AMOUNT_TOKEN for _ in range(amount_count - amount_column - 1)
        )
        pattern = (
            r"^\s*" + date_pattern + suffix + column_gap + description_pattern
            + column_gap + preceding_amounts + r"(?P<amount>" + _AMOUNT_TOKEN + r")"
            + trailing_amounts + trailing_suffix
        )
    section_mode = adjustments.get(
        "section_mode", "automatic" if "*start*transaction detail" in text.lower() else "all"
    )
    if section_mode not in {"all", "automatic", "between"}:
        raise StatementParseError("Choose where transaction rows appear")
    start_after = _guided_phrase(adjustments.get("start_after"), "Start text")
    end_before = _guided_phrase(adjustments.get("end_before"), "End text")
    automatic_sections = "*start*transaction detail" in text.lower()
    suggestion = {
        "transaction_pattern": pattern,
        "date_format": date_format,
        "outflow_sign": outflow_sign,
        "selected_rows": rows[:3],
        "description_excludes": _guided_phrase_list(adjustments.get("skip_descriptions")),
        "guidance": [f"Detected {date_label}."],
    }
    if positioned_debit_credit is not None:
        suggestion["debit_credit_columns"] = positioned_debit_credit
    if separate_debit_credit:
        suggestion["guidance"].append(
            "Detected separate debit and credit columns. Debits become spending; credits become money received."
        )
    elif amount_count >= 2:
        suggestion["guidance"].append(
            f"Using the {_ordinal(amount_column + 1)} money value in each row."
        )
    else:
        suggestion["guidance"].append(
            "Treating positive amounts as charges."
            if outflow_sign == "positive"
            else "Keeping the statement's negative charge amounts."
        )
    if date_order_choices:
        suggestion["guidance"].append(
            "The selected slash dates are ambiguous, so confirm whether the first number is month or day."
        )
    if wrapped_descriptions:
        suggestion["guidance"].append("Detected descriptions that continue onto the following line.")
    if trailing_reference:
        suggestion["guidance"].append("Ignored trailing reference values after the transaction amount.")
    period = _infer_statement_period(text, date_format)
    if period:
        suggestion["statement_period"] = period
        suggestion["guidance"].append(
            "Found the statement date range and will use it to recover transaction years."
        )
    if section_mode == "automatic" and automatic_sections:
        suggestion["transaction_start"] = r"\*start\*transaction detail"
        suggestion["transaction_end"] = r"\*end\*transaction detail"
        suggestion["guidance"].append("Found repeated transaction-detail sections.")
    elif section_mode == "between":
        if not start_after and not end_before:
            raise StatementParseError("Enter text before or after the transaction rows")
        if start_after:
            suggestion["transaction_start"] = _literal_phrase_pattern(start_after)
        if end_before:
            suggestion["transaction_end"] = _literal_phrase_pattern(end_before)

    suggestion["controls"] = {
        "date_columns": date_columns,
        "date_column": (requested_date_column or "first") if date_columns else "",
        "date_order_choices": date_order_choices,
        "date_order": (adjustments.get("date_order") or "month_first")
        if date_order_choices else "",
        "amount_columns": amount_choices,
        "amount_column": str(amount_column),
        "amount_mode": "debit_credit" if separate_debit_credit else "signed",
        "wrapped_descriptions": wrapped_descriptions,
        "trailing_reference": trailing_reference,
        "outflow_sign": outflow_sign,
        "section_mode": section_mode,
        "start_after": start_after,
        "end_before": end_before,
        "skip_descriptions": suggestion["description_excludes"],
        "automatic_sections": automatic_sections,
    }
    return suggestion


def _detect_csv_source(path, rows=None):
    """Identify known CSV exports by their header, rather than their filename."""
    try:
        for row in rows if rows is not None else _read_csv_rows(path):
            if "Datetime" in row:
                required = {"Datetime", "Amount (total)", "From", "To", "Note"}
                return "venmo" if required <= set(row) else None
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return None


def _generic_profiles():
    """Load user-defined CSV mappings without allowing executable configuration."""
    raw = load_json(RULES / "import_profiles.json", {})
    if not isinstance(raw, dict):
        raise JsonFileError("rules/import_profiles.json must be an object")
    profiles = {}
    for source, profile in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", source) or source in SOURCE_ADAPTERS:
            raise JsonFileError(f"invalid import profile source: {source!r}")
        if not isinstance(profile, dict):
            raise JsonFileError(f"import profile {source!r} must be an object")
        columns = profile.get("columns")
        required = {"date", "description"}
        signed_amount = isinstance(columns, dict) and "amount" in columns
        debit_credit = isinstance(columns, dict) and {"debit", "credit"} <= set(columns)
        if not isinstance(columns, dict) or required - set(columns) or signed_amount == debit_credit:
            raise JsonFileError(
                f"import profile {source!r} needs date and description plus either "
                "an amount column or debit and credit columns"
            )
        required_columns = required | ({"amount"} if signed_amount else {"debit", "credit"})
        if not all(isinstance(columns[key], str) and columns[key].strip()
                   for key in required_columns):
            raise JsonFileError(f"import profile {source!r} has invalid column names")
        account = profile.get("account")
        if not isinstance(account, str) or not account.strip():
            raise JsonFileError(f"import profile {source!r} needs an account name")
        sign = profile.get("outflow_sign", "negative")
        if sign not in {"negative", "positive"}:
            raise JsonFileError(
                f"import profile {source!r} outflow_sign must be negative or positive"
            )
        account_type = _account_type(
            profile.get("account_type"), f"import profile {source!r}"
        )
        fingerprints = profile.get("fingerprints", [])
        if (not isinstance(fingerprints, list)
                or not all(isinstance(value, str) and value.strip()
                           for value in fingerprints)):
            raise JsonFileError(f"import profile {source!r} has invalid fingerprints")
        profiles[source] = {
            "account": account.strip(),
            "columns": {key: columns[key].strip() for key in required_columns},
            "date_format": profile.get("date_format"), "outflow_sign": sign,
            "amount_mode": "signed" if signed_amount else "debit_credit",
            "account_type": account_type,
            "fingerprints": [value.strip().upper() for value in fingerprints],
        }
    return profiles


def _ofx_profiles():
    raw = load_json(RULES / "ofx_profiles.json", {})
    if not isinstance(raw, dict):
        raise JsonFileError("rules/ofx_profiles.json must be an object")
    profiles = {}
    for source, profile in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", source) or source in SOURCE_ADAPTERS:
            raise JsonFileError(f"invalid OFX profile source: {source!r}")
        if (not isinstance(profile, dict) or not isinstance(profile.get("account"), str)
                or not profile["account"].strip()):
            raise JsonFileError(f"OFX profile {source!r} needs an account")
        fingerprints = profile.get("fingerprints", [])
        if (not isinstance(fingerprints, list) or not fingerprints
                or not all(isinstance(x, str) and x.strip() for x in fingerprints)):
            raise JsonFileError(f"OFX profile {source!r} has invalid fingerprints")
        profiles[source] = {"account": profile["account"].strip(),
                            "fingerprints": [x.strip().upper() for x in fingerprints],
                            "account_type": _account_type(
                                profile.get("account_type"), f"OFX profile {source!r}"
                            )}
    return profiles


def validate_custom_source_maps(profiles, templates, ofx_profiles):
    """Reject a source ID shared by more than one configurable importer."""
    custom_source_maps = {
        "CSV profile": profiles,
        "OFX/QFX profile": ofx_profiles,
        "PDF template": templates,
    }
    seen_custom_sources = {}
    for kind, configured in custom_source_maps.items():
        for source in configured:
            if source in seen_custom_sources:
                raise JsonFileError(
                    f"custom source {source!r} is defined by both "
                    f"{seen_custom_sources[source]} and {kind}"
                )
            seen_custom_sources[source] = kind


def _parse_ofx_legacy(text, path, source, profile):
    """Read a minimally formed OFX file when a provider omits standard wrappers."""
    txns = []
    for block in re.findall(r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>)", text, re.I | re.S):
        fields = {name.upper(): value.strip() for name, value in
                  re.findall(r"<(DTPOSTED|TRNAMT|NAME|MEMO)>\s*([^<\r\n]+)", block, re.I)}
        if "DTPOSTED" not in fields or "TRNAMT" not in fields:
            continue
        try:
            when = dt.datetime.strptime(fields["DTPOSTED"][:8], "%Y%m%d").date()
            amount = float(fields["TRNAMT"].replace(",", ""))
        except ValueError as exc:
            raise StatementParseError(f"Invalid {source} OFX transaction in {path.name}") from exc
        txns.append({
            "date": when.isoformat(),
            "description": fields.get("NAME") or fields.get("MEMO") or "OFX transaction",
            "amount": amount,
            "account": profile["account"],
            "account_type": profile.get("account_type", "checking"),
            "source": source,
            "statement": path.name,
        })
    return txns


def _parse_ofx_with_library(path, source, profile):
    """Parse a standards-compliant OFX/QFX export with ofxparse."""
    from ofxparse import OfxParser

    try:
        with path.open("rb") as f:
            document = OfxParser.parse(f)
    except Exception as exc:
        raise _OfxStructureError(f"Invalid {source} OFX/QFX export in {path.name}") from exc
    if not document.accounts:
        raise _OfxStructureError(f"No standard account found in {path.name}")
    if len(document.accounts) != 1:
        raise StatementParseError(
            f"{path.name} contains {len(document.accounts)} accounts; export one account per file"
        )
    transactions = document.accounts[0].statement.transactions
    txns = []
    for transaction in transactions:
        when = getattr(transaction, "date", None)
        amount = getattr(transaction, "amount", None)
        if not isinstance(when, (dt.date, dt.datetime)) or amount is None:
            raise StatementParseError(f"Invalid {source} OFX transaction in {path.name}")
        description = (
            getattr(transaction, "payee", None)
            or getattr(transaction, "memo", None)
            or "OFX transaction"
        )
        txns.append({
            "date": when.date().isoformat() if isinstance(when, dt.datetime) else when.isoformat(),
            "description": str(description).strip(),
            "amount": float(amount),
            "account": profile["account"],
            "account_type": profile.get("account_type", "checking"),
            "source": source,
            "statement": path.name,
        })
    return txns


def _parse_ofx(path, source, profile, text=None):
    text = _decode_import_text(path) if text is None else text
    upper = text.upper()
    if not all(item in upper for item in profile["fingerprints"]):
        return None
    try:
        txns = _parse_ofx_with_library(path, source, profile)
    except _OfxStructureError:
        # Some providers label a file ".ofx" but omit the headers/wrappers the
        # standard permits. Preserve compatibility with those minimally formed
        # exports without making the handwritten reader the normal code path.
        txns = _parse_ofx_legacy(text, path, source, profile)
    if not txns:
        raise StatementParseError(f"No {source} OFX transactions found in {path.name}")
    record_coverage(source, min(dt.date.fromisoformat(t["date"]) for t in txns),
                    max(dt.date.fromisoformat(t["date"]) for t in txns),
                    profile.get("account_type", "checking"))
    return txns


def _parse_generic_amount(value):
    value = value.strip()
    negative = (value.startswith("-")
                or (value.startswith("(") and value.endswith(")"))
                or value.upper().endswith("DR"))
    digits = re.sub(r"[^\d.]", "", value)
    if not digits or digits.count(".") > 1:
        raise ValueError(value)
    amount = float(digits)
    return -amount if negative else amount


def _csv_profile_positions(rows, profile):
    headers = set(profile["columns"].values())
    header_idx = next((i for i, row in enumerate(rows) if headers <= set(row)), None)
    if header_idx is None:
        return None
    return header_idx, {name: rows[header_idx].index(name) for name in headers}


def _csv_profile_matches(rows, profile, document_text=None):
    """Match a CSV by its required columns and optional distinctive text."""
    if _csv_profile_positions(rows, profile) is None:
        return False
    fingerprints = profile.get("fingerprints", [])
    if not fingerprints:
        return True
    if document_text is None:
        document_text = "\n".join("\t".join(row) for row in rows).upper()
    return all(fingerprint in document_text for fingerprint in fingerprints)


def _parse_generic_csv(path, source, profile, rows=None):
    rows = _read_csv_rows(path) if rows is None else rows
    header = _csv_profile_positions(rows, profile)
    if header is None:
        return None
    header_idx, positions = header
    formats = [profile["date_format"]] if isinstance(profile["date_format"], str) else [
        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y",
    ]
    txns = []
    for row_no, row in enumerate(rows[header_idx + 1:], header_idx + 2):
        if len(row) <= max(positions.values()):
            continue
        date_s = row[positions[profile["columns"]["date"]]].strip()
        if not date_s:
            continue
        when = None
        for fmt in formats:
            try:
                when = dt.datetime.strptime(date_s, fmt).date()
                break
            except ValueError:
                continue
        if when is None:
            raise StatementParseError(f"Invalid {source} date on row {row_no} in {path.name}")
        try:
            if profile["amount_mode"] == "debit_credit":
                debit_value = row[positions[profile["columns"]["debit"]]].strip()
                credit_value = row[positions[profile["columns"]["credit"]]].strip()
                debit = _parse_generic_amount(debit_value) if debit_value else 0.0
                credit = _parse_generic_amount(credit_value) if credit_value else 0.0
                if debit and credit:
                    raise StatementParseError(
                        f"Ambiguous {source} debit and credit values on row {row_no} in {path.name}"
                    )
                if not debit and not credit:
                    continue
                amount = -abs(debit) if debit else abs(credit)
            else:
                amount = _parse_generic_amount(row[positions[profile["columns"]["amount"]]])
        except ValueError as exc:
            raise StatementParseError(f"Invalid {source} amount on row {row_no} in {path.name}") from exc
        if profile["amount_mode"] == "signed" and profile["outflow_sign"] == "positive":
            amount = -amount
        txns.append({
            "date": when.isoformat(),
            "description": row[positions[profile["columns"]["description"]]].strip(),
            "amount": amount, "account": profile["account"],
            "account_type": profile.get("account_type", "checking"), "source": source,
            "statement": path.name,
        })
    if not txns:
        raise StatementParseError(f"No transactions found for {source} in {path.name}")
    record_coverage(source, min(dt.date.fromisoformat(t["date"]) for t in txns),
                    max(dt.date.fromisoformat(t["date"]) for t in txns),
                    profile.get("account_type", "checking"))
    return txns


# This is the parser registry. Adding a future importer means adding an adapter
# here, its format detector, and tests; folder configuration stays separate.
SOURCE_ADAPTERS = {
    "chase": {"pattern": "*.pdf", "parse": "parse_chase"},
    "csp": {"pattern": "*.pdf", "parse": "parse_csp"},
    "vx": {"pattern": "*.pdf", "parse": "parse_vx"},
    "venmo": {"pattern": "*.csv", "parse": "parse_venmo"},
}

# Declarative equivalents of the built-in parsers. These are intentionally
# exposed only for non-persistent calibration: they show how a supported layout
# is modeled and provide a concrete starting point for an unfamiliar PDF.
REFERENCE_PDF_TEMPLATES = {
    "chase": {
        "account": "Chase Checking",
        "account_type": "checking",
        "fingerprints": ["Chase", "*start*transaction detail"],
        "transaction_pattern": (
            r"^\s*(?P<date>\d{2}/\d{2})\s+(?P<description>.*?)\s+"
            r"(?P<amount>-?[\d,]*\.\d{2})\s+-?[\d,]*\.\d{2}\s*$"
        ),
        "transaction_start": r"\*start\*transaction detail",
        "transaction_end": r"\*end\*transaction detail",
        "statement_period": {
            "pattern": (
                r"(?P<start>[A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+"
                r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})"
            ),
            "date_format": "%B %d, %Y",
        },
        "date_format": "%m/%d",
        "outflow_sign": "negative",
    },
    "csp": {
        "account": "Sapphire Preferred",
        "account_type": "credit_card",
        "fingerprints": ["Opening/Closing Date", "Sapphire Preferred"],
        "transaction_pattern": (
            r"^\s*(?P<date>\d{2}/\d{2})\s{2,}(?P<description>.*?)\s{2,}"
            r"(?P<amount>-?[\d,]*\.\d{2})\s*$"
        ),
        "statement_period": {
            "pattern": (
                r"Opening/Closing\s+Date\s+(?P<start>\d{2}/\d{2}/\d{2})\s*-\s*"
                r"(?P<end>\d{2}/\d{2}/\d{2})"
            ),
            "date_format": "%m/%d/%y",
        },
        "summary": {
            "outflows": [
                r"Purchases\s+\+?\$?(?P<amount>[\d,]+\.\d{2})",
                r"Fees Charged\s+\+?\$?(?P<amount>[\d,]+\.\d{2})",
            ],
            "credits": [r"Payment,?\s*Credits\s+-?\$?(?P<amount>[\d,]+\.\d{2})"],
            "allow_missing_credits": True,
        },
        "date_format": "%m/%d",
        "outflow_sign": "positive",
    },
    "vx": {
        "account": "Venture X",
        "account_type": "credit_card",
        "fingerprints": ["Capital One", ": Transactions"],
        "transaction_pattern": (
            r"^\s*(?P<date>[A-Z][a-z]{2}\s+\d{1,2})\s+[A-Z][a-z]{2}\s+\d{1,2}\s+"
            r"(?P<description>.*?)\s+(?P<amount>-?\s?\$[\d,]*\.\d{2})\s*$"
        ),
        "statement_period": {
            "pattern": (
                r"(?P<start>[A-Z][a-z]{2} \d{1,2}, \d{4})\s*-\s*"
                r"(?P<end>[A-Z][a-z]{2} \d{1,2}, \d{4})\s*\|"
            ),
            "date_format": "%b %d, %Y",
        },
        "summary": {
            "outflows": [
                r"Transactions\s+\+\s*\$?(?P<amount>[\d,]+\.\d{2})",
                r"Fees Charged\s+\+\s*\$?(?P<amount>[\d,]+\.\d{2})",
            ],
            "credits": [r"Payments\s+-\s*\$?(?P<amount>[\d,]+\.\d{2})"],
            "allow_extra_credits": True,
            "allow_missing_credits": True,
        },
        "date_format": "%b %d",
        "outflow_sign": "positive",
    },
}


def reference_pdf_template(source):
    """Return a JSON-safe copy of the matching built-in parser configuration."""
    template = REFERENCE_PDF_TEMPLATES.get(source)
    return json.loads(json.dumps(template)) if template is not None else None


def _adapter_parser(adapter):
    """Resolve a code-owned parser name, while allowing test adapters as callables."""
    parser = adapter["parse"]
    return globals()[parser] if isinstance(parser, str) else parser


def _inbox_files(root):
    """Yield visible inbox files recursively, including unsupported extensions."""
    if not root.is_dir():
        return []
    return sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        ),
        key=lambda path: str(path).lower(),
    )


def discover_known_pdf_files(folders=None, inbox=None):
    """Find supported PDF statements from legacy folders and the mixed inbox.

    Legacy folders explicitly select their parser. Inbox files are selected only
    after their contents match a known PDF fingerprint. The result is unique by
    resolved path and suitable for both parsing and reconciliation.
    """
    folders = source_folders() if folders is None else folders
    inbox = IMPORTS if inbox is None else inbox
    found, seen = [], set()
    for source, adapter in SOURCE_ADAPTERS.items():
        if adapter["pattern"] != "*.pdf" or source not in folders:
            continue
        directory = DOWNLOADS / folders[source]
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.pdf")):
            found.append((source, path))
            seen.add(path.resolve())
    for path in _inbox_files(inbox):
        if path.suffix.lower() != ".pdf" or path.resolve() in seen:
            continue
        source = _detect_pdf_source(pdftext(path))
        if source is not None:
            found.append((source, path))
    return found


def _parse_inbox_file(path, profiles, templates, ofx_profiles):
    """Return (source, transactions, approval_needed, extracted_text)."""
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".csv", ".ofx", ".qfx"}:
        return None, [], False, None
    if suffix == ".pdf":
        text = pdftext(path)
        source = _detect_pdf_source(text)
        if source is not None:
            return source, _adapter_parser(SOURCE_ADAPTERS[source])(path, text), False, text
        source = _template_pdf_source(text, templates)
        if source is None:
            return None, [], False, text
        # A template can be valid but not match this particular statement. Stage
        # before parsing so an unapproved mismatch cannot abort a full refresh.
        return source, None, True, text
    if suffix in {".ofx", ".qfx"}:
        text = _decode_import_text(path)
        matches = [
            source for source, profile in ofx_profiles.items()
            if all(item in text.upper() for item in profile["fingerprints"])
        ]
        if len(matches) != 1:
            return None, [], False, None
        source = matches[0]
        # A profile is explicit user configuration, analogous to an adapter:
        # saving it is the review step. File-level approval is reserved for
        # inferred PDF templates created through the guided UI.
        return source, _parse_ofx(path, source, ofx_profiles[source], text), False, None
    rows = _read_csv_rows(path)
    source = _detect_csv_source(path, rows)
    if source is not None:
        return source, _adapter_parser(SOURCE_ADAPTERS[source])(path), False, None
    document_text = (
        "\n".join("\t".join(row) for row in rows).upper()
        if any(profile.get("fingerprints") for profile in profiles.values())
        else None
    )
    matches = [
        name for name, profile in profiles.items()
        if _csv_profile_matches(rows, profile, document_text)
    ]
    if len(matches) != 1:
        return None, [], False, None
    source = matches[0]
    return source, _parse_generic_csv(path, source, profiles[source], rows), False, None


# ---------------------------------------------------------------------------
_ID_FIELDS = ("source", "account", "date", "datetime", "description",
              "amount", "counterparty", "note")


def _transaction_identity(txn):
    """Content that identifies a transaction independently of its statement file."""
    return tuple(txn.get(field, "") for field in _ID_FIELDS)


def _migration_identity(value, index):
    """Validate one reviewed target identity from identity_migrations.json."""
    if not isinstance(value, dict):
        raise JsonFileError(
            f"identity migration {index} new_identity must be an object"
        )
    unknown = set(value) - set(_ID_FIELDS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise JsonFileError(
            f"identity migration {index} new_identity has unknown field(s): {names}"
        )
    required = {"source", "account", "date", "description", "amount"}
    missing = required - set(value)
    if missing:
        names = ", ".join(sorted(missing))
        raise JsonFileError(
            f"identity migration {index} new_identity is missing: {names}"
        )

    normalized = {field: value.get(field, "") for field in _ID_FIELDS}
    for field in _ID_FIELDS:
        if field == "amount":
            amount = normalized[field]
            if (isinstance(amount, bool) or
                    not isinstance(amount, (int, float)) or
                    not math.isfinite(amount)):
                raise JsonFileError(
                    f"identity migration {index} new_identity amount must be finite"
                )
            normalized[field] = float(amount)
        elif not isinstance(normalized[field], str):
            raise JsonFileError(
                f"identity migration {index} new_identity {field} must be a string"
            )
    if any(not normalized[field].strip()
           for field in ("source", "account", "date", "description")):
        raise JsonFileError(
            f"identity migration {index} needs non-empty source, account, date, and description"
        )
    try:
        dt.date.fromisoformat(normalized["date"])
    except ValueError as exc:
        raise JsonFileError(
            f"identity migration {index} new_identity date must be YYYY-MM-DD"
        ) from exc
    return tuple(normalized[field] for field in _ID_FIELDS)


def _reviewed_identity_migrations(existing, candidate_unique, enabled):
    """Return approved old-ID assignments for intentional identity corrections.

    Normal refreshes never call this path. When explicitly enabled, every
    mapping must identify exactly one prior ledger row and exactly one corrected
    parser row. Reusing the historical ID preserves every user-owned overlay
    that is keyed by transaction ID.
    """
    if not enabled:
        return {}, set(), defaultdict(Counter)

    raw = load_json(RULES / "identity_migrations.json", None)
    if not isinstance(raw, list) or not raw:
        raise JsonFileError(
            "rules/identity_migrations.json must be a non-empty list when "
            "--apply-identity-migrations is used"
        )

    existing_by_id = defaultdict(list)
    existing_by_identity = defaultdict(list)
    for txn in existing:
        if not isinstance(txn, dict) or not isinstance(txn.get("id"), str):
            continue
        existing_by_id[txn["id"]].append(txn)
        existing_by_identity[_transaction_identity(txn)].append(txn)
    candidate_counts = Counter(_transaction_identity(txn) for txn in candidate_unique)

    assignments = {}
    migrated_ids = set()
    allowed_missing = defaultdict(Counter)
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise JsonFileError(f"identity migration {index} must be an object")
        if set(item) != {"old_id", "new_identity", "reviewed", "reason"}:
            raise JsonFileError(
                f"identity migration {index} must contain old_id, new_identity, "
                "reviewed, and reason"
            )
        old_id = item["old_id"]
        if not isinstance(old_id, str) or not old_id.strip():
            raise JsonFileError(f"identity migration {index} old_id must be a string")
        if item["reviewed"] is not True:
            raise JsonFileError(
                f"identity migration {index} must set reviewed to true"
            )
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise JsonFileError(f"identity migration {index} needs a review reason")
        if old_id in migrated_ids:
            raise JsonFileError(f"identity migration {index} repeats old_id {old_id!r}")
        old_rows = existing_by_id.get(old_id, [])
        if len(old_rows) != 1:
            raise JsonFileError(
                f"identity migration {index} old_id {old_id!r} must identify "
                "exactly one existing transaction"
            )

        old_identity = _transaction_identity(old_rows[0])
        new_identity = _migration_identity(item["new_identity"], index)
        if old_identity == new_identity:
            raise JsonFileError(
                f"identity migration {index} does not change the normalized identity"
            )
        if len(existing_by_identity[old_identity]) != 1:
            raise JsonFileError(
                f"identity migration {index} cannot select one of multiple "
                "historically identical transactions"
            )
        if candidate_counts[old_identity]:
            raise JsonFileError(
                f"identity migration {index} old identity is still emitted by "
                "the parser; remove this migration"
            )
        if existing_by_identity[new_identity]:
            raise JsonFileError(
                f"identity migration {index} target identity already exists in "
                "the historical ledger"
            )
        if candidate_counts[new_identity] != 1:
            raise JsonFileError(
                f"identity migration {index} target identity must match exactly "
                "one newly parsed transaction"
            )
        if new_identity in assignments:
            raise JsonFileError(
                f"identity migration {index} repeats a target identity"
            )

        assignments[new_identity] = old_id
        migrated_ids.add(old_id)
        allowed_missing[old_rows[0].get("source", "")] += Counter({old_identity: 1})
    return assignments, migrated_ids, allowed_missing


def _existing_ids_by_identity(path, protected_ids=()):
    """Index a prior ledger so an ID-format upgrade cannot orphan user rules."""
    existing = load_json(path, [])
    if not isinstance(existing, list):
        return {}
    ids = defaultdict(list)
    for txn in existing:
        if isinstance(txn, dict) and isinstance(txn.get("id"), str):
            ids[_transaction_identity(txn)].append(txn["id"])
    protected_ids = set(protected_ids)
    return {
        identity: sorted(values, key=lambda value: (value not in protected_ids, value))
        for identity, values in ids.items()
    }


def _protected_transaction_ids():
    """Return parsed transaction IDs referenced by user-owned overlay rules.

    When an identical statement was briefly imported twice, both copies can
    exist in the prior ledger with different IDs. Reusing an arbitrary ID would
    orphan reimbursements, trip membership, and other user edits after the
    duplicate is removed. Prefer the ID that those rules already reference.
    """
    protected = set()
    for name in ("reimbursed.json", "deleted.json", "reviewed.json"):
        values = load_json(RULES / name, [])
        if isinstance(values, list):
            protected.update(str(value) for value in values)
    for name in ("trip_flags.json", "trip_members.json", "txn_overrides.json",
                 "desc_overrides.json", "amortize.json"):
        values = load_json(RULES / name, {})
        if isinstance(values, dict):
            protected.update(str(value) for value in values)
    groups = load_json(RULES / "groups.json", [])
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("members"), list):
                protected.update(str(value) for value in group["members"])
    return protected


def _deduplicate_and_assign_ids(txns, existing_ids=None, migrated_ids=None):
    """Collapse overlapping statement copies while retaining real duplicates.

    A repeated statement may contain the same transaction rows as a prior
    statement. For each canonical transaction identity, the highest occurrence
    count in any one statement is the number of real transactions to preserve.
    The ordinal within that count distinguishes genuinely repeated purchases and
    makes IDs stable across filename changes and input ordering.
    """
    by_identity = defaultdict(lambda: defaultdict(list))
    first_seen = {}
    for index, txn in enumerate(txns):
        identity = _transaction_identity(txn)
        first_seen.setdefault(identity, index)
        by_identity[identity][txn["statement"]].append(txn)

    existing_ids = existing_ids or {}
    migrated_ids = migrated_ids or {}
    unique = []
    for identity in sorted(by_identity, key=first_seen.__getitem__):
        by_statement = by_identity[identity]
        occurrences = max(len(rows) for rows in by_statement.values())
        statements = sorted(by_statement)
        for ordinal in range(occurrences):
            # Keep one representative's provenance for reconciliation. Its file
            # name is intentionally not part of the ID payload.
            txn = next(by_statement[statement][ordinal]
                       for statement in statements
                       if len(by_statement[statement]) > ordinal)
            item = dict(txn)
            # One ledger row may be present in more than one overlapping
            # statement. Retain every source file that contained this ordinal
            # so statement-level reconciliation remains exact without adding
            # duplicate transactions to the dashboard ledger.
            provenance = [
                statement for statement in statements
                if len(by_statement[statement]) > ordinal
            ]
            if len(provenance) > 1:
                item["statements"] = provenance
            prior_ids = existing_ids.get(identity, [])
            if identity in migrated_ids:
                item["id"] = migrated_ids[identity]
            elif ordinal < len(prior_ids):
                item["id"] = prior_ids[ordinal]
            else:
                payload = repr(identity + (ordinal + 1,))
                item["id"] = hashlib.sha1(payload.encode()).hexdigest()[:16]
            unique.append(item)
    unique.sort(key=lambda txn: (txn["date"], txn["source"]))
    return unique


def main(apply_identity_migrations=False):
    # main() is also called from tests and the local server process. Do not let
    # coverage or balance anchors from an earlier invocation leak into this run.
    COVERAGE.clear()
    CHASE_BALANCES.clear()
    all_txns = []
    import_report = {"imported": [], "duplicates": [], "staged": [], "unsupported": []}
    approvals = load_json(RULES / "import_approvals.json", {})
    if not isinstance(approvals, dict):
        raise JsonFileError("rules/import_approvals.json must be an object")
    existing = load_json(DATA / "transactions.json", [])
    existing_sources = {
        t.get("source") for t in existing
        if isinstance(t, dict) and isinstance(t.get("source"), str)
    }
    folders = source_folders()
    profiles = _generic_profiles()
    templates = _pdf_templates()
    raw_templates = load_json(RULES / "pdf_templates.json", {})
    if not isinstance(raw_templates, dict):
        raise JsonFileError("rules/pdf_templates.json must be an object")
    ofx_profiles = _ofx_profiles()
    validate_custom_source_maps(profiles, templates, ofx_profiles)
    source_files = {source: 0 for source in SOURCE_ADAPTERS}
    source_files.update({source: 0 for source in profiles})
    source_files.update({source: 0 for source in templates})
    source_files.update({source: 0 for source in ofx_profiles})
    seen_paths = set()
    seen_contents = set()
    for source, adapter in SOURCE_ADAPTERS.items():
        if source not in folders:
            continue  # inbox-only adapter; no legacy organized folder configured
        folder, glob, fn = folders[source], adapter["pattern"], _adapter_parser(adapter)
        d = DOWNLOADS / folder
        if not d.exists():
            print(f"{folder}: missing -> skipped (no existing {source} transactions)")
            continue
        files = sorted(d.glob(glob))
        count = 0
        for f in files:
            digest = file_digest(f)
            if digest in seen_contents:
                print(f"{folder}/{f.name}: duplicate content -> skipped")
                continue
            got = fn(f)
            # A bare filename is not unique across legacy source folders. Keep
            # source-qualified provenance for reconciliation while transaction
            # IDs remain filename-independent.
            for txn in got:
                txn["statement"] = f"{source}:{f.name}"
            all_txns.extend(got)
            source_files[source] += 1
            seen_paths.add(f.resolve())
            seen_contents.add(digest)
            count += len(got)
        print(f"{folder}: {len(files)} files -> {count} txns")

    for path in _inbox_files(IMPORTS):
        if path.resolve() in seen_paths:
            continue
        name = str(path.relative_to(IMPORTS))
        if path.suffix.lower() not in {".pdf", ".csv", ".ofx", ".qfx"}:
            import_report["unsupported"].append(name)
            print(f"imports/{name}: unsupported -> skipped")
            continue
        digest = file_digest(path)
        if digest in seen_contents:
            import_report["duplicates"].append(name)
            print(f"imports/{name}: duplicate content -> skipped")
            continue
        source, got, needs_approval, text = _parse_inbox_file(
            path, profiles, templates, ofx_profiles
        )
        if source is None:
            import_report["unsupported"].append(name)
            print(f"imports/{name}: unsupported -> skipped")
            continue
        approval_config = (
            raw_templates.get(source)
            or profiles.get(source)
            or ofx_profiles.get(source)
        )
        if needs_approval and not import_is_approved(
                approvals, digest, source, approval_config):
            import_report["staged"].append({"file": name, "source": source})
            print(f"imports/{name}: {source} -> staged for approval")
            continue
        if got is None:
            got = _parse_template_pdf(path, text, source, templates[source])
        # The mixed inbox is recursive. Preserve its relative path so two
        # distinct files named ``statement.pdf`` cannot be treated as duplicate
        # rows from one source document during ledger de-duplication.
        for txn in got:
            txn["statement"] = name
        all_txns.extend(got)
        source_files[source] += 1
        seen_contents.add(digest)
        import_report["imported"].append({"file": name, "source": source})
        print(f"imports/{name}: {source} -> {len(got)} txns")

    # Validate the same set of rows that will actually be written. Comparing
    # raw parser output here would miss a loss caused by statement-overlap
    # deduplication (for example one real duplicate split across two PDFs).
    candidate_unique = _deduplicate_and_assign_ids(all_txns)
    migration_ids, migrated_old_ids, allowed_missing = _reviewed_identity_migrations(
        existing, candidate_unique, apply_identity_migrations
    )
    if migration_ids:
        print(f"Applying {len(migration_ids)} reviewed identity migration(s)")

    for source in existing_sources:
        remaining = [
            txn for txn in existing
            if txn.get("source") == source and txn.get("id") not in migrated_old_ids
        ]
        if source_files.get(source, 0) == 0 and remaining:
            locations = (
                f"{folders[source]} or imports/" if source in folders
                else "imports/ or the source configuration that originally imported it"
            )
            raise FileNotFoundError(
                f"No statements found for existing {source} transactions in "
                f"{locations}"
            )
    # Do not silently replace a historical ledger with the subset of statements
    # that happens to remain on disk. A user can add fresh files incrementally,
    # but removing even one already-imported statement must be an explicit
    # recovery/migration operation rather than an accidental data-loss refresh.
    existing_identities = defaultdict(Counter)
    for txn in existing:
        if not isinstance(txn, dict) or not isinstance(txn.get("source"), str):
            continue
        existing_identities[txn["source"]][_transaction_identity(txn)] += 1
    parsed_identities = defaultdict(Counter)
    for txn in candidate_unique:
        parsed_identities[txn["source"]][_transaction_identity(txn)] += 1
    for source, prior_identities in existing_identities.items():
        missing = prior_identities - parsed_identities[source] - allowed_missing[source]
        if missing:
            missing_count = sum(missing.values())
            raise FileNotFoundError(
                f"Missing {missing_count} previously imported {source} transaction(s). "
                "Restore the historical statements before refreshing so the ledger "
                "is not replaced with an incomplete subset."
            )

    # Extraction/parsing above completes before any artifact is written. A
    # pdftotext failure propagates, so existing ledgers are never replaced with a
    # partial parse.
    existing_ids = _existing_ids_by_identity(
        DATA / "transactions.json", _protected_transaction_ids())
    unique = _deduplicate_and_assign_ids(all_txns, existing_ids, migration_ids)
    out = DATA / "transactions.json"
    artifacts = (out, DATA / "coverage.json", DATA / "balances.json",
                 DATA / "import_report.json")
    before = {path: path.read_bytes() for path in artifacts if path.exists()}
    try:
        write_json(out, unique)
        write_json(DATA / "coverage.json", COVERAGE)
        # Roll statement-close balances to true calendar month-end (statements close
        # mid-month). Uses `unique` (the raw ledger, which reconciles to the penny).
        month_end_bals = _calendar_month_end_balances(unique, CHASE_BALANCES)
        write_json(DATA / "balances.json", dict(sorted(month_end_bals.items())))
        write_json(DATA / "import_report.json", import_report)
    except Exception:
        for path in artifacts:
            if path in before:
                write_bytes(path, before[path])
            elif path.exists():
                path.unlink()
        raise
    print(f"\nTOTAL: {len(unique)} unique transactions -> {out}")
    # Quick date-range sanity.
    if unique:
        print(f"range: {unique[0]['date']} .. {unique[-1]['date']}")
    for src, c in COVERAGE.items():
        print(f"coverage {src}: {c['start']} .. {c['end']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["--apply-identity-migrations"]):
        raise SystemExit(
            "usage: python3 parse.py [--apply-identity-migrations]"
        )
    main(apply_identity_migrations=bool(args))
