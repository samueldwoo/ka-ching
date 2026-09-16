# Rules and Personalization Guide

Ka-ching treats source statements as immutable evidence. Parsing creates a
normalized ledger; categorization and every manual edit are applied afterward as
small files under `rules/`. Refreshing statements rebuilds generated data and
reapplies those rules.

`rules/` is private user data. Back it up and never commit it to a public repo.

## Prefer the UI

Use the dashboard for routine changes:

- click a category pill to change one transaction or the merchant;
- accept or correct rows in the review queue;
- add, edit, or delete manual transactions;
- group charges with reimbursements;
- set trip flags, assignments, and reimbursed expenses;
- rename descriptions and amortize large purchases;
- set budgets and investing preferences.

These actions validate values and write the correct overlay schema. Hand-edit JSON
only for bulk keyword rules, account profiles, and settings documented below.

## Categorization precedence

Each imported transaction is categorized in this order:

1. a single-transaction override in `txn_overrides.json`;
2. a merchant override in `overrides.json`;
3. the first matching keyword rule in `categories.json`;
4. a configured fallback in `settings.json`;
5. neutral low-confidence fallback (`Miscellaneous` for unmatched outflows).

Low-confidence rows remain visible in the review queue. User overrides always win
and survive future statement refreshes.

## Keyword categories

Start from `examples/rules/categories.json` and copy it to
`rules/categories.json`. The schema is an ordered list:

```json
{
  "rules": [
    {
      "category": "Groceries",
      "match": ["EXAMPLE MARKET", "GROCERY"]
    },
    {
      "category": "Transportation",
      "match": ["TRANSIT", "RIDESHARE"]
    }
  ]
}
```

Matching is case-insensitive. Most entries are plain substrings, not regular
expressions. First match wins, so put narrow product/merchant rules before broad
terms. Avoid short ambiguous fragments. Review several real descriptions before
adding a keyword and refresh after editing.

Canonical categories are defined in `common.py`. JSON category values must match
one exactly. `Income`, `Transfers`, and `Savings & Investing` are excluded from
spending totals.

Wallet descriptions receive special protection: keyword matching uses the note
when available rather than a counterparty's name, reducing accidental category
matches against a person's name.

## Settings

`rules/settings.json` can enable location-based trip detection and explicit
low-confidence defaults:

```json
{
  "home_state": "NY",
  "fallback_categories": {
    "venmo": "Dining",
    "wallet": "Miscellaneous",
    "credit_card": "Miscellaneous"
  }
}
```

- `home_state` must be a two-letter US state or `DC`. If omitted, automatic
  domestic trip detection is disabled; manual trip controls still work.
- `fallback_categories` is optional. Supported keys are `venmo`, `wallet`, and
  `credit_card`, and values must be spending categories. These remain
  low-confidence and flagged for review.

Do not choose `Income`, `Transfers`, or `Savings & Investing` as an outflow
fallback. Prefer neutral `Miscellaneous` until your usage pattern is proven.

## Account locations

The mixed `imports/` inbox needs no folder configuration. Optional
`rules/sources.json` only renames legacy folders for built-in adapters:

```json
{
  "chase": "accounts/checking",
  "csp": "accounts/rewards-card",
  "vx": "accounts/travel-card",
  "venmo": "accounts/wallet"
}
```

Use `/` for nested folders on every platform. Values must stay inside the
project, and folder names must be distinct even when letter case is ignored.

Keys choose existing parsers. Changing a path does not teach the parser a new
institution. Use [PARSER_GUIDE.md](PARSER_GUIDE.md) for new layouts.

## Import rules

The parser-related files are:

| File | Purpose |
|---|---|
| `import_profiles.json` | CSV column mappings |
| `ofx_profiles.json` | OFX/QFX account identity and fingerprints |
| `pdf_templates.json` | Guided PDF templates saved by the UI |
| `import_approvals.json` | Exact file/template approvals |
| `identity_migrations.json` | Rare reviewed parser identity migrations |

Source IDs are durable transaction identity, not display labels. Keep them stable
and unique across profile types. Back up templates and approvals together.

## Overlay file map

| File | Durable behavior |
|---|---|
| `manual_txns.json` | Manually entered transactions |
| `deleted.json` | Hidden statement rows |
| `overrides.json` | Whole-merchant category choices |
| `txn_overrides.json` | One-transaction category choices |
| `desc_overrides.json` | Friendly transaction descriptions |
| `reviewed.json` | Accepted low-confidence rows |
| `groups.json` | Netted transaction groups |
| `reimbursed.json` | Work-paid expenses excluded from personal spend |
| `amortize.json` | Multi-month smoothing settings |
| `trip_flags.json` | Manual trip yes/no overrides |
| `manual_trips.json` | User-created trips |
| `trip_members.json` | Explicit trip assignments |
| `budgets.json` | Monthly category budgets |
| `paycheck.json` | Temporary take-home override |
| `invest.json` | Investing plan selection |

Do not edit IDs by hand. Transaction IDs connect these files to statement rows.
The app carries overlays from a matching manual pre-entry to the official row when
a later statement replaces it.

## Safe adjustment workflow

1. Back up `rules/` and source statements.
2. Change one profile or rule family at a time.
3. Refresh and read any parser error instead of deleting old source files.
4. Review category totals and every low-confidence row.
5. Reconcile supported statements.
6. Confirm a second refresh produces the same transaction count and totals.
7. Keep the backup until at least one later statement imports successfully.

Malformed existing JSON is an error and is never silently replaced with an empty
default. Repair or restore the file rather than deleting it when it contains
important edits.

## Sharing diagnostics safely

Before sharing an issue or fixture, replace names, account numbers, addresses,
transaction descriptions, and balances while preserving the layout characteristics
needed to reproduce the bug. Do not upload `rules/`, `data/`, screenshots, raw
console logs, or statements without reviewing them for financial and identity data.
