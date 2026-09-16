# Getting Started

This guide takes a clean Ka-ching checkout to a local dashboard and explains how
to add data without committing financial information.

## Requirements

| Requirement | Status |
|---|---|
| Python 3.10-3.14 | Required; 3.13 recommended |
| Modern browser | Required |
| Internet during setup | Required for package installation |
| Poppler `pdftotext` | Recommended but optional; PyMuPDF and pypdf are fallbacks |
| Git | Optional when using a ZIP download |

All Python packages are installed into the project-local `.venv`. Run commands
from the folder containing `app.py`, `requirements.txt`, and `scripts/`; that
folder may have any name.

## Install and run

### macOS

The setup script finds a compatible Python or installs Homebrew Python 3.13. It
also attempts to install optional Poppler.

```bash
./scripts/setup-macos.sh
./scripts/run-macos.sh
```

If a ZIP lost executable bits, run `chmod +x scripts/*.sh`. To choose an existing
interpreter:

```bash
KA_CHING_PYTHON=/path/to/python3.11 ./scripts/setup-macos.sh
```

### Windows 10 or 11

Use Windows PowerShell 5.1 or newer. The setup supports x64, x86, and ARM64 and
can install Python 3.13 for the current user through `winget`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1
```

To select an installed interpreter:

```powershell
$env:KA_CHING_PYTHON = "C:\Path\To\python.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

### Ubuntu 22.04+ or Debian 12+

```bash
sudo apt update
sudo apt install git python3 python3-venv poppler-utils
./scripts/setup-linux.sh
./scripts/run-linux.sh
```

Poppler is optional; omit `poppler-utils` when it is unavailable.

## Confirm the first launch

Open [http://localhost:8000](http://localhost:8000). A clean checkout displays an
empty dashboard and creates local `data/` and `rules/` paths as needed. The server
binds to `127.0.0.1`, not your LAN address. Press Ctrl+C to stop it.

Setup uses a project lock, validates exact dependency pins with `pip check`, and
restores the last completed `.venv` when an update is interrupted. Activation is
not required; always use the platform run script.

## Add the first data

### Manual entry

Open Transactions, choose **Add transaction**, and enter a date, description,
account, category, and signed amount. Spending is negative; money received is
positive. The entry is written to `rules/manual_txns.json`.

### Statement inbox

Create one inbox. Files may be mixed or placed in arbitrary subfolders.

```bash
mkdir -p imports
```

```powershell
New-Item -ItemType Directory -Force imports | Out-Null
```

Drop PDF, CSV, OFX, or QFX exports there, open the app, and choose **Refresh
data**. Unknown layouts are reported in Imports and `data/import_report.json`;
they are not guessed. Replace corrupt/encrypted exports and OCR image-only PDFs
locally before retrying.

Built-in detectors recognize Chase checking, Chase Sapphire Preferred, Capital
One Venture X, and Venmo exports. For every other account, follow
[PARSER_GUIDE.md](PARSER_GUIDE.md). Prefer OFX/QFX, then CSV, then guided PDF.

## Personalize safely

1. Copy only the examples you need from `examples/rules/` into a new local
   `rules/` directory.
2. Set `home_state` in `rules/settings.json` to enable automatic domestic trip
   detection. Without it, trip detection remains manual.
3. Refresh, open the review queue, and correct low-confidence categories.
4. Add stable, unambiguous keywords to `rules/categories.json` only after
   reviewing several statements.
5. Set budgets in the Budgets tab and verify detected income before relying on
   investing suggestions.

See [RULES_GUIDE.md](RULES_GUIDE.md) for precedence, schemas, and backup advice.

## Verify an import

Inspect the first and last transaction, amount signs, refunds, statement dates,
and unmatched rows. For supported PDFs, run:

```bash
.venv/bin/python reconcile.py
```

```powershell
& .\.venv\Scripts\python.exe reconcile.py
```

`Problems: 0. Unverified: 0.` means every discovered supported statement passed
its available independent total or balance check. A reviewed custom PDF without
printed-total rules is intentionally reported as unverified.

## Back up local state

| Path | Meaning | Backup priority |
|---|---|---|
| `imports/` | Original statement exports | Required |
| `rules/` | Manual edits, profiles, templates, trips, budgets | Required |
| `data/` | Generated ledger and reports | Optional but useful |
| `.venv/` | Installed dependencies | None; rebuild it |

All four are ignored by Git in the public repository. Common statement extensions
are ignored globally as an additional guard. Before every push, run `git status`
and confirm only source, documentation, examples, or synthetic tests are staged.

## Update and test

After pulling source changes, rerun platform setup so dependency drift is checked.
For development tests:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt -c constraints.txt
.venv/bin/python -m pytest -q
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## Troubleshooting

| Problem | Resolution |
|---|---|
| Python version rejected | Install Python 3.10-3.14 and rerun setup; 3.13 is recommended |
| `.venv` is stale | Rerun the platform setup script |
| Port 8000 is in use | Stop the other local process, then rerun |
| PDF extraction fails | Replace corrupt/encrypted files or OCR scanned pages locally |
| File is unsupported | Open Imports and configure a reviewed profile or template |
| Categories look wrong | Review `rules/categories.json`, overrides, and settings |
| A month is incomplete | Restore every source statement, refresh, then reconcile |
| Setup download fails | Check network/proxy access and rerun; a valid old `.venv` is restored |
