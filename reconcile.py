#!/usr/bin/env python3
"""Reconcile parsed txns against each statement's printed summary totals.

Pulls the summary figures straight from each PDF and compares to the sum of
parsed transactions for that statement. Any mismatch > 1c is flagged.
"""
import re
from collections import defaultdict

from common import DATA, IMPORTS, RULES, file_digest, pdftext, money, load_json, source_folders
from parse import (
    _detect_pdf_source,
    _inbox_files,
    _pdf_templates,
    _template_pdf_source,
    discover_known_pdf_files,
    import_is_approved,
)


def _statement_key(path, source=None):
    """Match the provenance key written by parse.py for legacy or inbox files."""
    try:
        return str(path.resolve().relative_to(IMPORTS.resolve()))
    except ValueError:
        return f"{source}:{path.name}" if source else path.name


def _statement_rows(by_stmt, path, source=None):
    """Read new source-qualified provenance, with a one-way legacy fallback."""
    return by_stmt.get(_statement_key(path, source), by_stmt[path.name])


def _reconcile_template_summary(source, path, text, template, transactions):
    """Check a reviewed custom PDF's configured totals against its ledger rows."""
    summary = template.get("summary")
    if not summary:
        print(f"UNVERIFIED {path.name}: no printed summary rules configured for {source}")
        return 0, 1, 0
    checked = problems = 0
    unverified = False
    for kind in ("outflows", "credits"):
        patterns = summary[kind]
        if not patterns:
            continue
        expected = 0.0
        missing = False
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                missing = True
                break
            expected += abs(money(match["amount"]))
        if missing:
            if kind == "credits" and summary["allow_missing_credits"]:
                continue
            print(f"UNVERIFIED {path.name}: missing {source} {kind} summary")
            unverified = True
            continue
        actual = (
            sum(-txn["amount"] for txn in transactions if txn["amount"] < 0)
            if kind == "outflows"
            else sum(txn["amount"] for txn in transactions if txn["amount"] > 0)
        )
        mismatch = (
            expected - actual > 0.01
            if kind == "credits" and summary["allow_extra_credits"]
            else abs(expected - actual) > 0.01
        )
        checked = 1
        if mismatch:
            print(
                f"MISMATCH {path.name}: {source} {kind} parsed "
                f"{actual:.2f} vs printed {expected:.2f}"
            )
            problems += 1
    return checked, int(unverified or not checked), problems


def _reviewed_custom_pdfs():
    """Yield approved custom-template PDFs with their compiled summary rules."""
    templates = _pdf_templates()
    raw_templates = load_json(RULES / "pdf_templates.json", {})
    approvals = load_json(RULES / "import_approvals.json", {})
    if not isinstance(raw_templates, dict) or not isinstance(approvals, dict):
        return
    for path in _inbox_files(IMPORTS):
        if path.suffix.lower() != ".pdf":
            continue
        text = pdftext(path)
        if _detect_pdf_source(text) is not None:
            continue
        source = _template_pdf_source(text, templates)
        if source is None:
            continue
        raw_template = raw_templates.get(source)
        if (not isinstance(raw_template, dict)
                or not import_is_approved(
                    approvals, file_digest(path), source, raw_template
                )):
            continue
        yield source, path, text, templates[source]


def main():
    txns = load_json(DATA / "transactions.json", [])
    by_stmt = defaultdict(list)
    for t in txns:
        # A deduplicated ledger row can originate in overlapping statement
        # files. Reconcile it against each such file, but keep it once in the
        # dashboard ledger to avoid double-counting spend.
        statements = t.get("statements") or [t.get("statement")]
        for statement in set(statements):
            if statement:
                by_stmt[statement].append(t)

    problems = 0
    checked = 0
    skipped = 0
    discovered = 0
    discovered_files = []
    seen_contents = set()
    for source, path in discover_known_pdf_files(source_folders()):
        digest = file_digest(path)
        if digest in seen_contents:
            continue
        seen_contents.add(digest)
        discovered_files.append((source, path))

    # ---- CSP ----
    for source, p in discovered_files:
        if source != "csp":
            continue
        discovered += 1
        txt = pdftext(p)
        mp = re.search(r"Purchases\s+\+?\$?([\d,]+\.\d{2})", txt)
        mc = re.search(r"Payment,?\s*Credits\s+-?\$?([\d,]+\.\d{2})", txt)
        mf = re.search(r"Fees Charged\s+\+?\$?([\d,]+\.\d{2})", txt)
        got = _statement_rows(by_stmt, p, source)
        # Parsed outflows = purchases + fees; compare to printed Purchases + Fees.
        pur = sum(-x["amount"] for x in got if x["amount"] < 0)
        cred = sum(x["amount"] for x in got if x["amount"] > 0)
        printed_out = money(mp.group(1)) if mp else 0.0
        printed_out += money(mf.group(1)) if mf else 0.0
        if mp:
            checked += 1
            if abs(printed_out - pur) > 0.01:
                print(f"MISMATCH {p.name}: outflows parsed {pur:.2f} vs printed purchases+fees {printed_out:.2f}")
                problems += 1
        else:
            skipped += 1
        if mc and abs(money(mc.group(1)) - cred) > 0.01:
            print(f"MISMATCH {p.name}: credits parsed {cred:.2f} vs printed {mc.group(1)}")
            problems += 1

    # ---- VX ----
    for source, p in discovered_files:
        if source != "vx":
            continue
        discovered += 1
        txt = pdftext(p)
        mp = re.search(r"Transactions\s+\+\s*\$([\d,]+\.\d{2})", txt)
        mc = re.search(r"Payments\s+-\s*\$([\d,]+\.\d{2})", txt)
        mf = re.search(r"Fees Charged\s+\+\s*\$([\d,]+\.\d{2})", txt)
        got = _statement_rows(by_stmt, p, source)
        pur = sum(-x["amount"] for x in got if x["amount"] < 0)
        cred = sum(x["amount"] for x in got if x["amount"] > 0)
        printed_out = money(mp.group(1)) if mp else 0.0
        printed_out += money(mf.group(1)) if mf else 0.0
        if mp:
            checked += 1
            if abs(printed_out - pur) > 0.01:
                print(f"MISMATCH {p.name}: outflows parsed {pur:.2f} vs printed txns+fees {printed_out:.2f}")
                problems += 1
        else:
            skipped += 1
        # VX 'Payments' summary excludes 'Other Credits'; parsed credits may include
        # both, so only flag if parsed is LESS than printed payments (missing rows).
        if mc and money(mc.group(1)) - cred > 0.01:
            print(f"MISMATCH {p.name}: payments parsed {cred:.2f} vs printed {mc.group(1)}")
            problems += 1

    # ---- Chase ----
    for source, p in discovered_files:
        if source != "chase":
            continue
        discovered += 1
        txt = pdftext(p)
        got = _statement_rows(by_stmt, p, source)
        # Reconcile via balance identity: beginning + net == ending.
        mb = re.search(r"Beginning Balance\s+\$?(-?[\d,]+\.\d{2})", txt)
        me = re.search(r"Ending Balance\s+\$?(-?[\d,]+\.\d{2})", txt)
        if mb and me:
            checked += 1
            beg, end = money(mb.group(1)), money(me.group(1))
            net = sum(x["amount"] for x in got)
            if abs(beg + net - end) > 0.01:
                print(f"MISMATCH {p.name}: {beg:.2f} + net {net:.2f} = {beg+net:.2f} vs ending {end:.2f}")
                problems += 1
        else:
            skipped += 1

    # ---- Reviewed custom PDF templates ----
    # These templates are already validated during parsing. Repeating their
    # configured summary checks here independently catches ledger/provenance
    # regressions after a later refresh or categorization change.
    for source, path, text, template in _reviewed_custom_pdfs():
        digest = file_digest(path)
        if digest in seen_contents:
            continue
        seen_contents.add(digest)
        discovered += 1
        add_checked, add_skipped, add_problems = _reconcile_template_summary(
            source, path, text, template, by_stmt[_statement_key(path)]
        )
        checked += add_checked
        skipped += add_skipped
        problems += add_problems

    if discovered == 0:
        print("NO STATEMENTS DISCOVERED: reconciliation cannot verify an empty input")
        problems += 1
    if checked == 0:
        print("NO STATEMENTS CHECKED: no usable statement summary figures found")
        problems += 1
    print(
        f"\nChecked {checked} statements. Problems: {problems}. "
        f"Unverified: {skipped}."
    )
    return 1 if problems or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
