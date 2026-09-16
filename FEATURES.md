# What You Can Do — Ka-ching Feature Guide

A tour of everything the dashboard can do, from basic tracking to advanced trip
accounting and expense amortization. For setup instructions, see
[GETTING_STARTED.md](GETTING_STARTED.md).

The app is organized into six tabs (left rail): **Overview · Cash Flow · Trends ·
Trips · Transactions · Budgets**. A global **month selector** and **search** live
in the top context bar.

---

## 1. Track spending (the basics)

Once your statements are parsed, every transaction across your imported accounts
is merged into one normalized ledger and categorized automatically. The app ships
with content-detected Chase checking, Sapphire Preferred, Venture X, and Venmo
parsers, and supports reviewed CSV/OFX mappings and guided PDF templates for
other accounts.

- **Overview tab** — your monthly "front page": a masthead for the selected month,
  KPI tiles (Total Spending, Income, Saved/Invested, Net Cash Flow), a
  plain-English summary, spending-by-category bars, a daily-spend heatmap, and top
  merchants.
- **Month selector** (top bar) — step through any month; every tab reflects the
  chosen month.
- **Click any category bar** to list that category's transactions beside it; click
  the same category again to hide.

### Import statements

- **Imports** (header) — inspect files in the local `imports/` inbox. Built-in
  layouts are ready to refresh immediately.
- **Other CSV/OFX/QFX exports** — add a small reviewed mapping once; later files
  can stay in the same mixed inbox.
- **Other text-based PDFs** — select two or three transaction rows to build a
  draft, adjust it in plain language if needed, review the matches, save the
  template, then approve the specific file. The normal workflow never requires
  writing a regular expression.
- **Scanned PDFs** — OCR them locally before setup. Files that are not recognized
  are never imported speculatively.

### Avoiding double-counting

Your accounts overlap (a card charge, the checking payment to that card, a
bank-funded Venmo), so the app is careful:

- Imported **card purchases** are the source of truth for spending. Categorize
  each checking-account card-payment merchant as `Transfers` once so paying the
  card does not count as a second purchase; a whole-merchant override persists.
- Wallet reimbursements can net against the category of the expense they repay;
  a category can therefore go negative ("net reimbursed"). Use a transaction
  group when one purchase is repaid by several rows.
- Categorize brokerage contributions as `Savings & Investing`, which is shown as
  a percentage of income rather than spend.

---

## 2. Interpret & explore

- **Cash Flow tab** — a Sankey diagram: income sources → your money → where it went
  (savings + each spending category). Hover any flow for the amount; **click a
  flow** to list its underlying transactions. Windowed by month / YTD / 12-month.
- **Trends tab** — month-over-month charts: income vs spending vs saving, your
  end-of-month cash balance over time, a smoothed **investing rate** (with a
  Rate/Dollars toggle), and single-category history.
- **Daily spending heatmap** (Overview) — a calendar where each day's shade
  reflects spend; click a day to see that day's transactions.
- **"vs usual" context** — category bars show how the month compares to your
  trailing 6-month average, flagging meaningful deviations (e.g. "+38% vs usual").

---

## 3. Manual transactions (no statement required)

You don't need a bank statement to record something.

- **Transactions tab → ＋ Add transaction** — enter date, description, amount
  (negative = spending), and category. Perfect for cash purchases or a charge that
  hasn't hit a statement yet.
- **⋯ → 🗑 Delete / hide** — the per-row **⋯** menu removes a manual transaction,
  or hides a statement row (restorable).
- **Double-click a description** to give it a friendly display name.

All manual edits are stored as overlay files and **re-applied on every re-parse**,
so they survive future statement imports. If a real statement row later matches
your manual entry, they reconcile automatically.

---

## 4. Fix categories (your edits always win)

- **Click any category pill** in the Transactions tab to recategorize — either the
  **whole merchant** (all past & future charges) or **just that one transaction**.
- **Review queue** — low-confidence auto-guesses get a ⚑ flag and a "N to review"
  badge; click it to filter to just those, then accept as-is (⚑) or correct them.
- **Auto-rules** are plain keyword lists you can edit directly if you prefer.

Your overrides always take priority over the automatic rules.

---

## 5. Budgets & auto-invest planning

- **Budgets tab** — set a monthly budget per category. "Reset to suggested" seeds
  each from your trailing-average spend. On the in-progress month, active
  categories show an **end-of-month pace projection**.
- **Auto-invest planner** — models `invest = take-home − budget − cushion`. Pick a
  tier (Safe / Aggressive / Max) and it shows the per-paycheck auto-transfer, using
  your real pay cadence (biweekly = 26 checks/yr, detected from the ledger).
  **Windfalls** (RSU vests, lump deposits) get a separate sweep suggestion, and
  your **realized savings rate** (actual vs. plan) is shown.
- **Raise not in a statement yet?** Click the take-home figure to enter it
  manually; it auto-reverts once the ledger catches up.

---

## 6. Trips (2-D: category + trip)

Every spending transaction keeps its true category **and** an on-a-trip flag, so
travel spend is tracked in two dimensions at once.

- **Auto-detected trips** — the Trips tab clusters trip-flagged transactions by
  date, names them by location, and breaks each down by category.
- **✈ toggle** (per transaction) — mark whether a charge happened on a trip.
  Auto-set outside the home state only after `home_state` is configured in
  `rules/settings.json`; otherwise location-based detection stays off. Your
  manual toggle always overrides inference.
- **＋ New trip** — create a custom trip (e.g. "Big Sur road trip") and assign any
  transactions to it.
- **Category bar or legend** — filter an individual trip to one or more
  categories. Choose **Custom order** to drag trip cards by the `⠿` handle (or focus
  a handle and use `↑`/`↓`).
- **Trip vs Home** (Overview) — split the whole month's spending into at-home vs
  on-a-trip.
- **Trips-at-a-glance strip** — ranks trips by cost, with the reimbursed portion
  faded so your net out-of-pocket stands out.

### Work-trip reimbursements (per-expense)

For a trip where work pays you back, mark each **reimbursed** expense with the ↩
pill (or "Mark all reimbursed" then un-toggle exceptions). A reimbursed expense
**stays in the trip's gross total** (so the trip still shows what it cost) but
**drops out of your Overview/Budgets spend**. Assign the reimbursement deposit to
the trip and it reconciles automatically, flagging only a shortfall.

---

## 7. Reconciliation groups

When one charge is paid back by several transactions — you front a group dinner,
friends Venmo you back — **select the rows → ⧉ Group these**. The group **nets to
one effective amount** (charge − reimbursements), counted in the charge's month.
Members still appear individually in the table; only the aggregate math nets them.

---

## 8. Amortization (the "Smooth" lens)

Big lumpy expenses (annual insurance, a flight booked months ahead, a yearly
subscription) distort the month you paid them.

- **⋯ menu on a transaction → Amortize…** — spread that charge evenly across N
  months. It shows a `～ Nmo` badge.
- **～ Smooth toggle** (top bar) — turn the smoothing lens on/off globally. When
  **on**, amortized charges are prorated across the months they cover, so trends
  read smoothly. When **off**, you see exact cash-basis amounts that reconcile to
  your statements.

Smoothed view is clearly labeled as not reconciling to statements — it's an
analysis lens, not a change to the underlying ledger.

---

## 9. Scenario modeling ("what-if")

On the Overview, the **what-if panel** lets you model cutting spending:

- **Click category chips** to exclude them and instantly see the effect on the
  month's total and net cash flow.
- **Essentials only** — keep just the essentials, model dropping the rest.
- **Reset all** — clear the model.

A "Modeling · N hidden" badge in the header reminds you when a scenario is active.

---

## 10. Search & export

- **Search** (top bar, or press `/`) — token search across all transactions; jumps
  you to the Transactions tab with results filtered.
- **Filters** (Transactions tab) — by month (multi-select), category (multi-select),
  account (multi-select), plus needs-review-only, grouped-only and
  manual-entries-only toggles. (The trip-vs-home split is an Overview control — see
  §6.) All three multi-selects filter on the **effective** month/category, so a
  group pinned to a reporting month is reachable under that month.
- **✕ Clear filters (N)** — appears in the filter row as soon as any filter or
  search term is active, and states how many it will clear.
- **Show N more** — the table paints at most 800 rows at a time; the button below it
  pages in the rest. Changing a filter starts again from the first page.
- **↓ Export CSV** — download the current filtered view as a CSV. Always the whole
  filtered set, never just the rows currently painted, and named after the months
  actually selected.

---

## 11. Cash-on-hand accuracy

Bank statements close mid-month, so a raw "June balance" is really your balance on
~June 11. The app rolls each statement's printed ending balance forward through the
ledger to the **true last calendar day** of each month — drift-free, because the
raw ledger reconciles to each statement to the penny.

---

## Where your edits are stored

Durable financial edits are stored in small overlay files under `rules/` rather
than changing source statements or the parsed ledger. Categories, descriptions,
groups, reimbursements, trip assignments, budgets, and similar edits survive a
refresh. Hiding a statement row can be undone, but deleting a manually entered
transaction removes that entry permanently.

Display-only preferences such as the active tab, collapsed sections, and custom
trip order are stored in that browser's `localStorage`; they do not follow the
repository to another browser or device.

---

## Quick reference

| I want to… | Where |
|---|---|
| See this month's spending | Overview tab |
| Understand where money flowed | Cash Flow (Sankey) |
| See month-over-month trends | Trends tab |
| Add a cash purchase | Transactions → ＋ Add transaction |
| Fix a wrong category | Click the category pill on any transaction |
| Set a budget | Budgets tab |
| Plan how much to invest | Budgets → auto-invest planner |
| Track a vacation's cost | Trips tab (auto) or ＋ New trip |
| Import a new statement layout | Header → Imports |
| Handle a fronted group expense | Select rows → ⧉ Group these |
| Smooth out a big annual charge | ⋯ → Amortize, then ～ Smooth |
| Model cutting spending | Overview → what-if chips |
| Find a specific transaction | Search (`/`) |
| Export data | Transactions → ↓ Export CSV |
