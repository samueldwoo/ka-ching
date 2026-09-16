# Statement Parser Guide

Ka-ching keeps import behavior separate from filenames and folders. Put PDF, CSV,
OFX, and QFX files anywhere under `imports/`; each file is identified from its
contents. Unknown or ambiguous files fail closed and remain out of the ledger.

This guide starts with the least fragile option and ends with the custom-code path.

## Choose the import path

| Available export | Recommended path | Code required |
|---|---|---|
| OFX or QFX | Reviewed OFX profile | No |
| CSV | Reviewed column profile | No |
| Text-based PDF | Guided Imports builder | No for common layouts |
| Scanned/image PDF | OCR locally, then guided builder | External OCR only |
| Unusual PDF table | Custom Python adapter | Yes |

Prefer a structured export even when a PDF is available. OFX and CSV preserve
fields directly; PDF parsing must reconstruct rows from positioned text.

## Terms used by profiles

- **Source ID** is a stable internal key such as `example_checking`. Use lowercase
  letters, digits, and underscores, beginning with a letter. It is stored on
  transaction identities, so do not rename it after importing history.
- **Account** is the display name shown in filters and transaction rows, such as
  `Everyday Checking`.
- **Account type** is `checking`, `credit_card`, or `wallet`. It controls generic
  fallback behavior and coverage checks; it does not select a parser.
- **Fingerprint** is stable institution/product text inside a file. It prevents a
  profile from claiming another export with the same columns or format.

A source ID must be unique across CSV, OFX/QFX, and PDF template configurations.

## CSV profile

1. Put one unedited export under `imports/`.
2. Open it in a plain-text or spreadsheet viewer and identify exact column names.
3. Create `rules/import_profiles.json` from the fictional example in
   `examples/rules/import_profiles.json`.
4. Refresh and inspect the imported file before adding older statements.

A signed-amount profile looks like this:

```json
{
  "example_checking": {
    "account": "Everyday Checking",
    "account_type": "checking",
    "columns": {
      "date": "Date",
      "description": "Description",
      "amount": "Amount"
    },
    "date_format": "%Y-%m-%d",
    "outflow_sign": "negative"
  }
}
```

Use `outflow_sign: "negative"` when purchases/withdrawals are already negative.
Use `"positive"` when the export prints spending as positive numbers. The parser
normalizes all money leaving an account to negative values.

For separate debit and credit columns, replace the `amount` entry:

```json
"columns": {
  "date": "Posted Date",
  "description": "Memo",
  "debit": "Debit",
  "credit": "Credit"
}
```

`date_format` is optional for ISO (`2026-07-15`) and common US dates. Set an
explicit Python `strptime` format for other layouts, for example `%d/%m/%Y`.

Profiles match required headers. If two accounts export identical headers, add
stable text found in the document:

```json
"fingerprints": ["Example Credit Union", "Everyday Checking"]
```

If more than one profile matches, the file remains unsupported rather than being
assigned to a guessed account.

## OFX/QFX profile

OFX/QFX parsing uses `ofxparse` for standard posted date, signed amount, payee,
and memo fields. A profile supplies the local account identity and requires a
stable file fingerprint:

```json
{
  "example_savings": {
    "account": "Example Savings",
    "account_type": "checking",
    "fingerprints": ["ORG>EXAMPLECU"]
  }
}
```

Save this as `rules/ofx_profiles.json`. Export one account per file. Inspect amount
signs and payee text on the first refresh. A minimally formed OFX file can use the
built-in fallback reader, but a valid standards-compliant export is preferred.

## Guided PDF template

The guided workflow is intended for a user who does not know regular expressions.
It derives technical rules from examples and plain-language adjustments.

1. Put a text-based PDF under `imports/` and open **Imports**.
2. Select the unsupported file.
3. Enter the account display name and account type.
4. Enter one or two phrases printed on every statement for that exact product.
   Choose product-specific text, not generic words such as `statement` or `bank`.
5. Select two or three complete purchase rows. Add a refund only when its visible
   format differs from purchases.
6. Choose **Build transaction draft**.
7. Under **Adjust the draft**, verify which date and amount columns are used,
   month/day order, debit-versus-credit meaning, transaction section boundaries,
   wrapped descriptions, trailing references, and recurring rows to skip.
8. Choose **Check draft**. Read every parsed transaction and every unmatched
   transaction-like row. Check the review confirmation only after reaching the
   end of both lists.
9. Save the template, reopen the recognized file, check it again, and approve the
   import. Refresh only after approval.

The builder supports common numeric, month-name, ISO, and day-first dates; one
wrapped description line; trailing reference columns; signed amounts; and
separate debit/credit columns. Its saved `rules/pdf_templates.json` contains
regular expressions as an implementation detail. Back up that file, but make
normal adjustments through the UI.

Approval is bound to the exact file digest, source ID, and template revision.
Changing a file or template requires a new review. This prevents a once-approved
layout from silently importing materially different statements.

### PDF review checklist

Before trusting a draft, verify:

- first and last transaction dates belong to the printed statement period;
- purchases are negative, payments/refunds are positive;
- every transaction page and section is represented;
- wrapped merchant names are joined but adjacent rows are not;
- fees, interest, payments, and credits are either intentionally included or
  intentionally excluded;
- no subtotal, rewards, balance, or page-header line became a transaction;
- unmatched rows contain no omitted transactions;
- transaction count and totals are plausible against the statement summary.

Printed charge and credit summary rules are optional because not every statement
publishes usable totals. Without them, a file can be exhaustively row-reviewed
and approved, but `reconcile.py` reports it as `Unverified`.

## Custom PDF adapter

Use a Python adapter only when the guided model cannot represent the layout. Work
from at least two redacted statements that cover purchases, refunds/payments, and
a year or statement-period boundary.

### Adapter contract

Add `parse_<institution>(path, text=None)` to `parse.py`. Reuse `pdftext(path)`
when text is not provided, validate the institution/product identity and statement
period, isolate transaction sections, then return normalized rows:

```python
{
    "date": "2026-07-15",
    "description": "EXAMPLE MARKET",
    "amount": -42.50,
    "account": "Example Rewards Card",
    "account_type": "credit_card",
    "source": "example_rewards",
    "statement": path.name,
}
```

Amounts leaving the account are negative. Incoming money, refunds, and card
payments are positive. Do not add categories in the parser; categorization is a
separate, user-adjustable stage.

Register the adapter in `SOURCE_ADAPTERS`:

```python
"example_rewards": {
    "pattern": "*.pdf",
    "parse": "parse_example_rewards"
}
```

Add a conservative signature to `_detect_pdf_source(text)`. Require institution
or product identity plus a layout-specific transaction marker. Return `None` when
uncertain. The mixed inbox needs no folder registration; add a default folder to
`DEFAULT_SOURCE_FOLDERS` only for optional legacy organization.

### Adapter validation

Synthetic or redacted fixtures should cover:

- purchases, fees, payments, refunds, and credits;
- December-to-January and partial date year assignment;
- multiple transaction sections and page headers;
- wrapped descriptions and unusual whitespace;
- overlapping statement periods and deterministic transaction IDs;
- changed layouts that must reject rather than partially parse;
- Poppler, PyMuPDF, and pypdf extraction where practical;
- independent printed totals or balance identities.

Never weaken identity checks to make one sample pass. A false negative leaves a
file staged for setup; a false positive can corrupt the ledger.

## Refresh and reconcile

The UI Refresh button is preferred. Command-line equivalents are:

```bash
.venv/bin/python parse.py && .venv/bin/python categorize.py
.venv/bin/python reconcile.py
```

```powershell
& .\.venv\Scripts\python.exe parse.py
if ($LASTEXITCODE -eq 0) {
    & .\.venv\Scripts\python.exe categorize.py
}
& .\.venv\Scripts\python.exe reconcile.py
```

Parsing rebuilds from all available source files and de-duplicates overlapping
statement periods. Once a source has history, refresh refuses to proceed if all
files for that source disappear. Restore the files instead of accepting a partial
ledger.

## Parser identity changes

Transaction-specific rules are keyed to deterministic transaction IDs. If a
parser correction changes a historical transaction's normalized identity, plain
refresh refuses the replacement. After reviewing both sides, use an explicit
one-to-one `rules/identity_migrations.json` entry and run
`parse.py --apply-identity-migrations`. This is a developer migration, not a
normal new-account step; preserve source, account, date, description, and amount
carefully.
