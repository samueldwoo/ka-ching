# Synthetic configuration examples

Every name, transaction, and amount in this directory is fictional.

- `imports/example-checking.csv` demonstrates a signed-amount CSV export.
- `rules/import_profiles.json` maps that CSV into normalized transactions.
- `rules/ofx_profiles.json` shows the account identity required for OFX/QFX.
- `rules/categories.json` shows ordered, case-insensitive keyword categories.
- `rules/settings.json` shows optional home-state and fallback configuration.

Create a local `rules/` directory and copy only the files you intend to adapt.
Replace account names, source IDs, column names, fingerprints, home state, and
keywords with values reviewed against your own exports. Do not put real examples
back into this tracked directory.
