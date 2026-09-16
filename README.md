# Ka-ching

Ka-ching is a local-first personal finance dashboard. It normalizes statement
exports into one ledger, supports manual transactions and durable corrections,
and provides spending, cash-flow, trend, trip, budget, and investing views.

Your statements and ledger stay on your computer. The server listens only on
`127.0.0.1`, and the running app makes no outbound requests.

## Quick start

Requires Python 3.10-3.14 (3.13 recommended) and a modern browser.

```bash
# macOS
./scripts/setup-macos.sh
./scripts/run-macos.sh

# Ubuntu/Debian, after installing python3 and python3-venv
./scripts/setup-linux.sh
./scripts/run-linux.sh
```

```powershell
# Windows 10/11
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1
```

Open [http://localhost:8000](http://localhost:8000). See
[GETTING_STARTED.md](GETTING_STARTED.md) for platform prerequisites,
troubleshooting, backups, and first-run validation.

## Add data

A manual-only dashboard needs no statement files. To import statements, create
`imports/` and place files anywhere below it. A mixed folder is supported; file
contents select the parser, not the filename or directory.

Built-in content detectors support:

- Chase checking PDFs
- Chase Sapphire Preferred PDFs
- Capital One Venture X PDFs
- Venmo CSV exports

Other accounts use one of three reviewed paths:

1. OFX/QFX: define the account identity once; standard fields are library-parsed.
2. CSV: map date, description, and amount or debit/credit columns in JSON.
3. Text PDF: use the Imports window to select sample rows, infer a draft, inspect
   every matched and unmatched row, then approve that file and template revision.

The normal PDF flow does not ask users to write regular expressions. A custom
Python adapter is available for layouts the guided builder cannot represent.
See [PARSER_GUIDE.md](PARSER_GUIDE.md).

## Adapt categories and behavior

Unmatched spending starts in `Miscellaneous` and is flagged for review. Correct a
transaction from the Transactions tab and choose whether the change applies to
that row or the merchant. Optional ordered keyword rules, home-state detection,
and source/account fallback categories are documented in
[RULES_GUIDE.md](RULES_GUIDE.md). Fictional starter files live under `examples/`.

## Privacy model

The public repository intentionally contains no statements, generated ledger, or
personal rules. These local paths are ignored by Git:

- `imports/`: source statements
- `data/`: generated transactions, balances, and reports
- `rules/`: durable edits, budgets, trips, and import profiles

The ignore file also blocks common statement extensions globally. Keep backups of
`imports/` and `rules/`; do not expose the local server through a public proxy
without adding authentication and TLS.

## License

Ka-ching source code is licensed under the
[GNU Affero General Public License v3.0](LICENSE). Modified versions that are
conveyed or offered to users over a network must make their corresponding source
available under the AGPL. This obligation concerns program source, not a user's
private statements or generated financial data.

Bundled fonts and installed packages retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the license files under
`assets/fonts/`.

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt -c constraints.txt
.venv/bin/python -m pytest -q
```

On Windows, use `.\.venv\Scripts\python.exe`. Public tests use synthetic data only.
The full feature tour is in [FEATURES.md](FEATURES.md).
