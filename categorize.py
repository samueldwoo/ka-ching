#!/usr/bin/env python3
"""
Categorize the parsed ledger.

Reads:
  data/transactions.json       (from parse.py)
  rules/categories.json        (ordered keyword rules)
  rules/overrides.json         (per-merchant overrides set in the UI; optional)

Writes:
  data/categorized.json        (transactions + category + confidence + flags)

Categorization order for each txn:
  1. Exact merchant override (rules/overrides.json)  -> confidence "user"
  2. First matching keyword rule                      -> confidence "rule"
  3. Configured or generic fallback                    -> confidence "low"

Fallbacks:
  * Unmatched outflows       -> "Miscellaneous" (flagged for review)
  * Optional source/account fallbacks can be set in rules/settings.json
  * Any inflow (amount > 0) not already Income/Transfer -> "Income"
"""
import bisect
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict

from common import CATEGORIES, DATA, RULES, NON_SPEND, load_json, write_json

# How many pre-entered manual txns the last apply_manual_layers() absorbed into
# matching official-statement rows (surfaced in the UI so the dedup is visible).
MANUAL_RECONCILED = 0

# Generic transfer/payment words do not identify the other party to a manual
# entry. They cannot by themselves reconcile two same-day, same-amount rows.
_MANUAL_MATCH_NOISE = frozenset({
    "payment", "purchase", "transfer", "transaction", "zelle", "venmo",
    "debit", "credit", "card", "cash", "tip",
})


def merchant_key(desc: str) -> str:
    """
    Normalize a description to a stable merchant key for overrides.

    Goal: two charges from the SAME store map to one key, while DIFFERENT
    stores stay distinct. The failure mode to avoid is over-stripping — an
    earlier greedy version reduced 'TST* EUREKA ...' and 'TST* PURE PROJECT ...'
    both to 'TST*', collapsing unrelated merchants. So we strip conservatively:
    only a clearly-trailing US state code (optionally preceded by ONE city
    token), never eating into the merchant name.

    Examples:
      'TST*RAMEN NAGI PALO ALTO Palo Alto CA' -> 'TST*RAMEN NAGI PALO ALTO'
      'IN-N-OUT SUNNYVALE SUNNYVALE CA'       -> 'IN-N-OUT SUNNYVALE'
      'SAFEWAY #1196SUNNYVALECA'              -> 'SAFEWAY #1196SUNNYVALECA' (no space -> left as-is)
    """
    d = re.sub(r"\s+", " ", desc.strip())
    # Strip a trailing standalone 2-letter uppercase state, with at most one
    # preceding city word. Requires the state to be its own space-delimited
    # token so we don't clip names ending in caps. Only ONE city word is
    # removed, bounding how much we can eat.
    d = re.sub(r"\s+[A-Z][A-Za-z.'-]*\s+[A-Z]{2}$", "", d)   # ' City ST'
    d = re.sub(r"\s+[A-Z]{2}$", "", d)                        # bare ' ST'
    return d.strip().upper()


def _reconciliation_tokens(description: str) -> set[str]:
    """Return normalized words useful for matching a manual note to a statement."""
    return {
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]{3,}", description.lower())
    }


def _manual_matches_statement(manual: dict, statement: dict) -> bool:
    """Whether a manual pre-entry is confidently represented by one statement row."""
    if (round(manual["amount"], 2) != round(statement["amount"], 2)
            or manual["date"] != statement["date"]):
        return False

    # A manually chosen account is a useful guard against two same-day, same-
    # amount purchases on different cards. The default "Manual" account remains
    # intentionally unbound for older entries that did not choose an account.
    manual_account = str(manual.get("account", "")).strip().casefold()
    statement_account = str(statement.get("account", "")).strip().casefold()
    if manual_account and manual_account != "manual" and manual_account != statement_account:
        return False

    manual_tokens = _reconciliation_tokens(manual.get("description", ""))
    statement_tokens = _reconciliation_tokens(statement.get("description", ""))
    distinctive = manual_tokens - _MANUAL_MATCH_NOISE
    if not distinctive:
        return False
    if distinctive & statement_tokens:
        return True

    # Keep the useful short-label case ("Coffee" entered manually vs "CAFE" on
    # the statement) without accepting a vague multi-word note such as "Cash
    # tip". Category agreement adds a second guard for this narrow fallback.
    return (len(distinctive) == 1
            and manual.get("category") == statement.get("category"))


# A charge with an out-of-home-state suffix or a recognized travel destination
# is treated as "on a trip". Auto-detection remains overridable per transaction
# through rules/trip_flags.json.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_US_STATE_SUFFIX = re.compile(r"\b(" + "|".join(sorted(_US_STATES)) + r")\s*$")
_DOMESTIC_DESTINATIONS = {"HONOLULU": "HI", "HALEIWA": "HI", "AIEA": "HI"}
_FOREIGN = re.compile(
    r"(SEOUL|TOKYO|KYOTO|OSAKA|OOSAKA|CHIBA|NARITA|INCHEON|GANGN|ITAEWON|"
    r"CIUDAD DE MEX|CDMX|CUAUHTEMOC|GUADALAJARA|PUEBLA|AMSTERDAM)",
    re.I,
)


def is_trip_location(description: str, home_state="CA") -> bool:
    """Return whether a merchant location appears outside ``home_state``."""
    home_state = str(home_state).strip().upper()
    if home_state not in _US_STATES:
        raise ValueError(
            "rules/settings.json home_state must be a two-letter US state or DC"
        )
    d = description.strip().upper()
    state = _US_STATE_SUFFIX.search(d)
    if state:
        return state.group(1) != home_state
    if _FOREIGN.search(d):
        return True
    return any(city in d and state_code != home_state
               for city, state_code in _DOMESTIC_DESTINATIONS.items())


# Generic short tokens that are ALSO common substrings of unrelated words and
# merchants — "GAS" in "LAS VEGAS", "AIR" in "FAIR OAKS", "BAR" left untouched
# would still be fine but "GU"/"TEA" collide with surnames and "green tea". These
# are matched on WORD BOUNDARIES (\bGAS\b) instead of as bare substrings, so they
# fire only on the standalone token. The needle text in categories.json is written
# with a trailing space ("GAS ", "AIR ") for readability; we key off the stripped
# token. Everything else stays a plain, fast substring match.
WORD_BOUNDARY_NEEDLES = {"GAS", "AIR", "BAR", "TEA", "PUB", "GU"}


def build_matcher(rules_doc):
    compiled = []
    for rule in rules_doc["rules"]:
        cat = rule["category"]
        needles = []
        for m in rule["match"]:
            mu = m.upper()
            if mu.strip() in WORD_BOUNDARY_NEEDLES:
                needles.append(re.compile(r"\b" + re.escape(mu.strip()) + r"\b"))
            else:
                needles.append(mu)
        compiled.append((cat, needles))
    return compiled


def _needle_hit(needle, text):
    """A needle matches as a plain substring, or on word boundaries when it's a
    precompiled regex (the generic-token needles above)."""
    if isinstance(needle, re.Pattern):
        return needle.search(text) is not None
    return needle in text


_FALLBACK_KEYS = {"venmo", "wallet", "credit_card"}


def configured_fallbacks(settings):
    """Validate optional low-confidence outflow defaults from settings.json."""
    raw = settings.get("fallback_categories", {})
    if not isinstance(raw, dict):
        raise ValueError("rules/settings.json fallback_categories must be an object")
    unknown = set(raw) - _FALLBACK_KEYS
    if unknown:
        names = ", ".join(repr(name) for name in sorted(unknown))
        raise ValueError(
            f"rules/settings.json fallback_categories has unsupported key(s): {names}"
        )
    for key, category in raw.items():
        if category not in CATEGORIES or category in NON_SPEND:
            raise ValueError(
                f"rules/settings.json fallback category for {key!r} must be a "
                "spending category"
            )
    return raw


def categorize_one(txn, compiled, overrides, mkey=None, fallbacks=None):
    desc_up = txn["description"].upper()
    if mkey is None:                       # caller may pass a precomputed key
        mkey = merchant_key(txn["description"])

    # 1. user override by merchant key
    if mkey in overrides:
        return overrides[mkey], "user", False

    # 2. keyword rules (first match wins). For Venmo, match ONLY the note (the text
    # after "Venmo in/out: NAME — "), never the counterparty's name — otherwise a
    # a counterparty's surname triggers a category via a generic needle.
    match_text = desc_up
    account_type = txn.get("account_type")
    if txn["source"] == "venmo" or account_type == "wallet":
        # Note is optional: for note-less rows ("Venmo out: NAME") we must NOT fall
        # back to matching the counterparty's NAME, so default to empty match_text.
        vm = re.match(r"^VENMO (?:IN|OUT):\s*[^—]*(?:—\s*(.*))?$", desc_up)
        if vm:
            match_text = vm.group(1) or ""
    for cat, needles in compiled:
        for n in needles:
            if _needle_hit(n, match_text):
                return cat, "rule", False

    # 3. fallbacks
    amt = txn["amount"]
    src = txn["source"]
    fallbacks = fallbacks or {}
    if src == "venmo":
        return fallbacks.get("venmo", "Miscellaneous"), "low", True
    if account_type == "wallet":
        # Generic wallets cannot safely assume a payment's purpose without a
        # matching keyword or user override.
        if amt > 0:
            return "Income", "low", True
        return fallbacks.get("wallet", "Miscellaneous"), "low", True
    if src in ("csp", "vx") or account_type == "credit_card":
        if amt > 0:
            return "Income", "rule", False  # a credit/refund on the card
        return fallbacks.get("credit_card", "Miscellaneous"), "low", True
    # chase
    if amt > 0:
        return "Income", "low", True
    return "Miscellaneous", "low", True


# Overlay files keyed by transaction id. When a pre-entered manual row is
# absorbed by the statement row that supersedes it (step 1c), every one of these
# would otherwise keep pointing at an id that no longer exists in the ledger —
# so the ↩ reimbursement mark, the ✈ trip flag, the ～ amortization, the friendly
# label, the category override and the trip assignment would all silently vanish.
_ID_KEYED_OVERLAYS = ("trip_flags.json", "amortize.json", "desc_overrides.json",
                      "txn_overrides.json", "trip_members.json")


def _rekey_overlays(remap, groups, rows):
    """Re-point id-keyed overlay files from absorbed manual pre-entries onto the
    statement rows that superseded them, and mirror the carried-over flags onto
    the in-memory rows so THIS run is already correct.

    `remap` is {absorbed_manual_id: statement_id}. Re-keying (rather than only
    patching memory) is what makes the flag round-trip: a ↩ pill whose id is
    absent from rules/reimbursed.json could never be un-marked again.
    """
    if not remap:
        return
    # A grouped row may be neither reimbursed nor amortized (app.py enforces both
    # directions), so those two flags are dropped rather than moved onto a member.
    grouped = {str(mid) for g in groups for mid in g.get("members", [])}
    by_id = {str(t.get("id")): t for t in rows if "id" in t}

    reimb = [str(x) for x in load_json(RULES / "reimbursed.json", [])]
    if any(tid in remap for tid in reimb):
        kept = []
        for tid in reimb:
            new = remap.get(tid, tid)
            if new in grouped and new != tid:
                continue                       # cannot be reimbursed while grouped
            if new not in kept:
                kept.append(new)
            row = by_id.get(new)
            if row is not None and new != tid:
                row["reimbursed"] = True
        write_json(RULES / "reimbursed.json", kept)

    for name in _ID_KEYED_OVERLAYS:
        data = load_json(RULES / name, {})
        if not isinstance(data, dict):
            continue
        moved = [k for k in data if str(k) in remap]
        if not moved:
            continue
        for k in moved:
            val = data.pop(k)
            new = remap[str(k)]
            if name == "amortize.json" and new in grouped:
                continue                       # cannot be amortized while grouped
            data.setdefault(new, val)          # an override set on the statement row wins
            row = by_id.get(new)
            if row is not None and name == "trip_flags.json":
                row["trip"] = bool(val) and row.get("category") not in NON_SPEND
        write_json(RULES / name, data)

    # A group whose member was a pre-entry keeps that member: follow the id.
    changed = False
    for g in groups:
        members = [str(mid) for mid in g.get("members", [])]
        if not any(m in remap for m in members):
            continue
        moved = []
        for m in members:
            new = remap.get(m, m)
            # never pull a row that already belongs to another group into this one
            if new != m and new in grouped:
                new = m
            if new not in moved:
                moved.append(new)
        if moved != members:
            g["members"] = moved
            changed = True
    if changed:
        write_json(RULES / "groups.json", groups)


def apply_manual_layers(out):
    """
    Apply user-owned overlay files on top of the categorized ledger. All three
    persist under rules/ so they survive future re-parses.

      rules/manual_txns.json  – transactions added by hand
      rules/deleted.json      – ids hidden/removed by the user
      rules/groups.json       – reconciliation groups {id, name, category, members[]}

    Grouping model (user's choice): a group nets to (sum of member amounts) and
    is attributed to the group's category and the EARLIEST member's month. Each
    member keeps its real date/amount for the drill-down but is marked
    grouped=True and carries group_id + agg_date so downstream spend math counts
    the net once, in the charge's month.
    """
    global MANUAL_RECONCILED
    MANUAL_RECONCILED = 0
    deleted = set(str(x) for x in load_json(RULES / "deleted.json", []))
    manual = load_json(RULES / "manual_txns.json", [])
    groups = load_json(RULES / "groups.json", [])
    # Manual rows never pass through the main categorize loop, so the per-txn
    # overlay flags it applies to parsed rows (reimbursed / trip / trip_auto) must
    # be applied here too — otherwise marking a hand-entered trip expense
    # reimbursed, or flagging it as trip spend via ✈, silently does nothing.
    trip_flags = load_json(RULES / "trip_flags.json", {})   # id -> bool (user overrides)
    reimbursed = set(str(x) for x in load_json(RULES / "reimbursed.json", []))  # expense ids paid back
    # Single-txn category overrides (rules/txn_overrides.json, keyed by txn id).
    # The main categorize loop applies these to PARSED rows; manual rows never pass
    # through it, so we must honor them here too — otherwise recategorizing a manual
    # txn via the inline dropdown (POST /api/txn_category) writes the override but the
    # manual row keeps its originally-stored category (silently reverts in the UI).
    txn_overrides = load_json(RULES / "txn_overrides.json", {})
    # Whole-merchant category overrides (rules/overrides.json, keyed by merchant_key).
    # Same reasoning: the UI offers "All <merchant> charges" whenever a manual row
    # shares a merchant with parsed ones, and without this the manual row was
    # repainted optimistically, toasted as done, then snapped back on the next load().
    merch_overrides = load_json(RULES / "overrides.json", {})

    # 1. append manual transactions (carry their own category/account; apply the
    #    same per-txn overlay flags the main loop applies to parsed rows).
    for m in manual:
        m = dict(m)
        m.setdefault("source", "manual")
        m.setdefault("account", "Manual")
        m.setdefault("confidence", "user")
        m.setdefault("needs_review", False)
        m["manual"] = True
        m["merchant_key"] = merchant_key(m.get("description", "manual"))
        tid = str(m.get("id"))
        # Precedence mirrors the parsed-row path in categorize_one: a whole-merchant
        # override beats the row's stored category, and the id-keyed override below
        # beats both.
        if m["merchant_key"] in merch_overrides:
            m["category"] = merch_overrides[m["merchant_key"]]
            m["confidence"] = "user"
            m["needs_review"] = False
        # A single-transaction override (set via the UI dropdown) wins over the
        # manual row's stored category — mirrors the main loop's txn_overrides check.
        if tid in txn_overrides:
            m["category"] = txn_overrides[tid]
            m["confidence"] = "user"
            m["needs_review"] = False
        # Per-expense WORK REIMBURSEMENT flag (rules/reimbursed.json): mirrors the
        # main loop so a manual trip expense marked reimbursed cancels out of spend.
        m["reimbursed"] = tid in reimbursed
        # Trip tag: manual rows have no location auto-detection, so the flag comes
        # solely from the user's manual ✈ (rules/trip_flags.json); trip_auto stays
        # False so a hand-flagged manual txn goes to the staging tray, not
        # auto-clustering. A NON_SPEND manual row is never trip spend.
        cat = m.get("category")
        m["trip"] = bool(trip_flags.get(tid, m.get("trip", False))) and cat not in NON_SPEND
        m["trip_auto"] = False
        out.append(m)

    # 1b. Drop duplicate Chase "Venmo Payment" rows. When your Venmo balance is
    # low, a Venmo payment draws from your bank, so the SAME spend appears twice:
    # once as the Venmo txn (with who/what) and once as a Chase "Venmo Payment"
    # (opaque ID only). Keep the informative Venmo row; suppress the Chase
    # duplicate when a Venmo outflow of the same amount exists within ±3 days.
    # Bucket Venmo outflows by rounded amount so each Chase-row lookup scans only
    # the same-amount candidates, not the whole list — turns the O(chase × venmo)
    # nested scan into ~O(n). Precompute each candidate's parsed date once. Within
    # a bucket, entries are consumed (popped) so each Venmo outflow cancels at most
    # one Chase dup — identical semantics to the prior linear claim-once scan.
    from collections import defaultdict as _dd
    venmo_buckets = _dd(list)
    for v in out:
        if v.get("source") == "venmo" and v["amount"] < 0:
            venmo_buckets[round(v["amount"], 2)].append(
                (dt.date.fromisoformat(v["date"]), v))
    def _claim_venmo_match(chase_row):
        cd = dt.date.fromisoformat(chase_row["date"])
        bucket = venmo_buckets.get(round(chase_row["amount"], 2))
        if not bucket:
            return False
        for i, (vd, _v) in enumerate(bucket):
            if abs((vd - cd).days) <= 3:
                bucket.pop(i)             # consume: each outflow cancels ONE Chase dup
                return True
        return False
    kept = []
    for t in out:
        if (t.get("source") == "chase"
                and re.search(r"venmo\s+payment", t["description"], re.I)
                and _claim_venmo_match(t)):
            continue   # duplicate of a Venmo txn we already have — drop it
        kept.append(t)
    out = kept

    # 1c. Reconcile PRE-ENTERED manual txns against the official statement. You can
    # pre-enter a purchase before the statement arrives (e.g. logging August as it
    # happens); when the real statement imports later it carries the same spend. A
    # manual row is DROPPED when a real (non-manual) statement row matches it on
    # amount (to the cent), exact date, compatible account, and a distinctive
    # description identifier. A counterparty name is enough: a manual note often
    # has context (who else attended, what was purchased) that a bank transfer
    # description omits. Each statement row cancels at most ONE manual entry (so
    # two identical charges don't both collapse into one). The statement (ground
    # truth) is kept. MANUAL_RECONCILED counts absorbed.
    real = [t for t in out if not t.get("manual")]
    claimed = set()                                   # ids of statement rows already used to cancel a manual
    kept2 = []
    remap = {}                                        # absorbed manual id -> statement id
    for t in out:
        if t.get("manual") and t.get("amount") is not None:
            match = next((r for r in real
                          if id(r) not in claimed
                          and _manual_matches_statement(t, r)), None)
            if match is not None:
                # Keep the statement's identity and amount as the ground truth,
                # but retain the user's manual category. A transfer description
                # commonly lacks enough merchant context to categorize itself
                # after the richer pre-entry has been reconciled away.
                if t.get("category"):
                    match["category"] = t["category"]
                    match["confidence"] = "user"
                    match["needs_review"] = False
                claimed.add(id(match))
                if t.get("id") is not None and match.get("id") is not None:
                    remap[str(t["id"])] = str(match["id"])
                MANUAL_RECONCILED += 1
                continue                              # official statement supersedes the pre-entry
        kept2.append(t)
    out = kept2
    # The category rides along above, but every OTHER overlay is keyed to the
    # manual id that just disappeared. Re-point them at the statement row (and
    # mirror them onto it in memory) so a reimbursed / ✈ / amortized / relabelled
    # / trip-assigned pre-entry keeps all of that when its statement imports.
    _rekey_overlays(remap, groups, out)

    # 2. hide deleted
    out = [t for t in out if str(t.get("id")) not in deleted]

    # 3. groups: net members, attribute to a reporting month + group category.
    # Default reporting month = the earliest member's (the charge) — correct for
    # reimbursements so the payback cancels the charge. A group may override this
    # with report_month ("YYYY-MM") to report in an event month instead (e.g. a
    # prepaid concert or a trip's flights booked months ahead).
    by_id = {str(t["id"]): t for t in out if "id" in t}
    for g in groups:
        members = [by_id[str(mid)] for mid in g.get("members", []) if str(mid) in by_id]
        if not members:
            continue
        rm = g.get("report_month")
        agg_date = (rm + "-01") if rm else min(m["date"] for m in members)
        gid = g.get("id")
        gcat = g.get("category")
        gtrip = g.get("trip")
        for m in members:
            m["group_id"] = gid
            m["group_name"] = g.get("name", "Group")
            m["grouped"] = True
            m["agg_date"] = agg_date                # month the net counts in
            m["report_month_set"] = bool(rm)
            if gcat:
                m["group_category"] = gcat
            if gtrip:                               # group marked as travel ->
                m["trip"] = True                    # every member counts as trip

    # 4. amortization tags: {txn_id: months}. Marks a charge to be spread over N
    # months (forward from its date) — used ONLY by the app's optional "smooth"
    # view. The real amount/date are untouched; cash-basis stays exact.
    amort = load_json(RULES / "amortize.json", {})
    for t in out:
        n = amort.get(str(t.get("id")))
        if n and int(n) > 1:
            t["amortize"] = int(n)
    return out


# Location tokens we can lift out of a description to name a trip.
_PLACES = [
    ("Seoul", r"SEOUL|GANGN|ITAEWON|INCHEON"), ("Tokyo", r"TOKYO|CHIBA|NARITA|OOSAKA"),
    ("Kyoto", r"KYOTO"), ("Osaka", r"OSAKA"), ("Mexico City", r"CIUDAD DE MEX|CDMX|CUAUHTEMOC"),
    ("Guadalajara", r"GUADALAJARA"), ("Puebla", r"PUEBLA"), ("Amsterdam", r"AMSTERDAM"),
    ("Hawaii", r"HONOLULU|HALEIWA|AIEA|\bHI\b"), ("New York", r"\bNY\b|BROOKLYN"),
    ("Seattle", r"SEATTLE|\bWA\b"), ("San Diego", r"SAN DIEGO|LA JOLLA|CORONADO"),
    ("Portland", r"\bOR\b"), ("Las Vegas", r"\bNV\b"),
]


def _place_of(desc):
    up = desc.upper()
    # Strip payment-processor location noise before matching — the "city" in
    # these is the vendor's HQ, NOT where you were:
    #   Uber:        "UBER *TRIP<CITY>" / "UBR* PENDING.UBER.COM<CITY>"  (Amsterdam)
    #   Booking.com: "BOOKING.COM ... AMSTERDAM" / "BKG*..."             (Amsterdam)
    up = re.sub(r"UB(ER|R)\s*\*?\s*(TRIP|PENDING\.UBER\.COM)\S*", " ", up)
    if "BOOKING.COM" in up or "BKG*" in up:
        up = up.replace("AMSTERDAM", " ")
    for name, pat in _PLACES:
        if re.search(pat, up):
            return name
    return "Trip"


def _trip_summary(tid, name, members):
    """Build one trip summary dict from its member txns; tags the members.
    Grouped members are NETTED together (charge + reimbursements count once) so
    a travel group contributes its net, not the gross sum of every member.
    UNgrouped reimbursements in a SPEND category (a lone Zelle/Venmo payback from
    a friend, tagged to the trip) net against their category — same as the
    Overview tab — so a $362 payback reduces trip Travel spend even without a
    reconciliation group. NON_SPEND members (a reimbursement deposit categorized
    Income/Transfer, if assigned to the trip) are skipped from the gross bars.

    WORK REIMBURSEMENT (per-expense model): each spend member carries a
    `reimbursed` bool (set from rules/reimbursed.json — you mark which specific
    expenses were paid back). The gross by_category shows what the trip COST
    (granularity preserved, reimbursed or not); `reimbursed` sums the expenses you
    marked paid-back; `net` = gross − reimbursed = true out-of-pocket. Reimbursed
    expenses are excluded from the Overview spend math (they cancel out — cash
    left, then came back), so only the un-reimbursed remainder (e.g. CHALOSEATAC)
    counts as real spend."""
    dates = sorted(m["date"] for m in members)
    cats = {}
    grp_net, grp_cat = {}, {}
    reimbursed = 0.0
    deposit_received = 0.0
    for m in members:
        m["trip_id"] = tid
        m["trip_name"] = name
        m["trip"] = True
        c = m.get("group_category") or m["category"]
        if c in NON_SPEND:                    # a reimbursement DEPOSIT assigned to the
            # trip (Income/Transfer inflow). Not gross spend and not income — it's
            # a reference figure: the money you actually got back. Tag it so the UI
            # renders it as a distinct "reimbursement received" row and auto-fills
            # "Got back $" for reconciliation against the per-expense reimbursed marks.
            m["is_reimbursement_deposit"] = True
            deposit_received += m["amount"]   # inflow, amount > 0
            continue
        m.pop("is_reimbursement_deposit", None)
        if m.get("reimbursed"):               # this specific expense was paid back
            reimbursed += -m["amount"]        # expense amount is negative -> positive
        gid = m.get("group_id")
        if gid:                              # accumulate the group's net
            grp_net[gid] = grp_net.get(gid, 0) + m["amount"]
            grp_cat[gid] = c
        else:                                # ungrouped: charges AND friend paybacks
            cats[c] = cats.get(c, 0) + -m["amount"]   # inflow (amount>0) subtracts
    for gid, net in grp_net.items():         # a group nets to one figure; a group
        c = grp_cat[gid]                     # reimbursed into profit reduces its cat
        cats[c] = cats.get(c, 0) + -net
    # GROSS per category (charges minus friend paybacks) — shows what the trip
    # cost, reimbursed or not. Drop cents-level noise.
    cats = {c: round(v, 2) for c, v in cats.items() if abs(v) > 0.005}
    by_category = {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}
    gross = round(sum(cats.values()), 2)
    net = round(gross - reimbursed, 2)
    out = {
        "id": tid, "name": name, "manual": tid.startswith("mtrip"),
        "start": dates[0], "end": dates[-1],
        "spend": gross, "count": len(members),          # spend = GROSS (per-category total)
        "reimbursed": round(reimbursed, 2),
        "net": 0.0 if net == 0 else net,                # true out-of-pocket
        "by_category": by_category,
    }
    if deposit_received > 0.005:                        # an assigned deposit auto-fills "Got back $"
        out["deposit_received"] = round(deposit_received, 2)
    return out


# Days of separation that end one auto-clustered trip and start the next.
GAP = 10


def _group_siblings(out):
    """Index reconciliation groups. Returns (gmembers, grp_of):
      gmembers: group_id -> [member txns]
      grp_of:   txn_id   -> that txn's group's member list
    Also propagates the trip flag across a group: if ANY member is on a trip,
    the whole group (charge + reimbursements) is, so the paybacks net against
    the trip's spend instead of floating loose. trip_auto propagates too, so a
    group with an auto-detected member is a clustering candidate as a unit."""
    gmembers = defaultdict(list)
    for t in out:
        if t.get("group_id"):
            gmembers[t["group_id"]].append(t)
    for g in gmembers.values():
        if any(m.get("trip") for m in g):
            auto = any(m.get("trip_auto") for m in g)
            for m in g:
                m["trip"] = True
                if auto:
                    m["trip_auto"] = True
    grp_of = {}
    for sibs in gmembers.values():
        for m in sibs:
            grp_of[str(m["id"])] = sibs
    return gmembers, grp_of


def _build_manual_trips(out, grp_of):
    """Pass 1: user-defined trips (rules/manual_trips.json + trip_members.json).
    Each explicitly-assigned txn brings its whole reconciliation group along,
    but a sibling pinned to a DIFFERENT manual trip stays put (explicit wins).
    Returns (summaries, assigned_ids)."""
    manual_trips = load_json(RULES / "manual_trips.json", [])   # [{id,name}]
    trip_members = load_json(RULES / "trip_members.json", {})   # txn_id -> trip_id
    by_id = {str(t.get("id")): t for t in out if "id" in t}
    summaries = []
    assigned = set()
    for mt in manual_trips:
        tid = mt["id"]
        direct = [k for k, v in trip_members.items() if v == tid and k in by_id]
        member_ids = set(direct)
        for k in direct:
            for sib in grp_of.get(k, []):
                sid = str(sib["id"])
                other = trip_members.get(sid)
                if other and other != tid:
                    continue        # pinned to another trip — leave it there
                member_ids.add(sid)
        members = [by_id[k] for k in member_ids if k in by_id]
        assigned.update(str(m["id"]) for m in members)
        if members:
            summaries.append(_trip_summary(tid, mt.get("name", "Trip"), members))
        else:
            # keep empty manual trips visible so you can add to them
            summaries.append({"id": tid, "name": mt.get("name", "Trip"),
                              "manual": True, "start": None, "end": None,
                              "spend": 0.0, "count": 0, "by_category": {}})
    return summaries, assigned


def _auto_cluster(out, assigned):
    """Pass 2: date-proximity clustering of the remaining auto-detected,
    trip-flagged outflows. A new trip starts when there was HOME spending
    between two trip charges (you came home) OR a dead gap > GAP days. A
    continuous stretch abroad (even across cities) stays one trip; multi-city
    splits/merges are done by hand. Only auto-detected on-location outflows
    drive boundaries — manual ✈ flags and reimbursement inflows don't."""
    rest = sorted((t for t in out if t.get("trip") and t.get("trip_auto")
                   and str(t.get("id")) not in assigned and t["amount"] < 0),
                  key=lambda t: t["date"])
    home_dates = sorted(dt.date.fromisoformat(x["date"]) for x in out
                        if x["amount"] < 0 and not x.get("trip")
                        and (x.get("group_category") or x.get("category")) not in NON_SPEND)

    def home_between(a, b):
        i = bisect.bisect_right(home_dates, a)
        return i < len(home_dates) and home_dates[i] < b

    clusters, cur = [], None
    for t in rest:
        d = dt.date.fromisoformat(t["date"])
        if cur and (d - cur["_last"]).days <= GAP and not home_between(cur["_last"], d):
            cur["members"].append(t); cur["_last"] = d
        else:
            cur = {"members": [t], "_last": d}; clusters.append(cur)
    return [cl for cl in clusters if cl["members"]]


def _consolidate_groups(clusters, gmembers, assigned):
    """Move each reconciliation group into ONE cluster so it nets as a unit. A
    group can be scattered — a book-ahead hotel charge, on-location charges,
    reimbursement inflows after you're home. Pull every sibling (including
    inflows that never landed in a cluster) into the cluster holding the
    plurality of the group's charge members, so paybacks offset that trip's
    spend. Siblings already claimed by a manual trip are left alone."""
    for gid, sibs in gmembers.items():
        counts = Counter(ci for ci, cl in enumerate(clusters)
                         for m in cl["members"] if m.get("group_id") == gid)
        if not counts:
            continue                       # no member landed in any real cluster
        home_cluster = counts.most_common(1)[0][0]
        for m in sibs:
            mid = str(m["id"])
            if mid in assigned:
                continue                   # belongs to a manual trip already
            for cl in clusters:            # pull it out of any other cluster
                cl["members"] = [x for x in cl["members"] if str(x["id"]) != mid]
            clusters[home_cluster]["members"].append(m)


def _name_cluster(cl):
    """Name an auto cluster from its members. A cluster with NO on-location
    activity (only Travel charges bought ahead of time) is an advance BOOKING;
    otherwise it's named by on-location month + the top visited cities."""
    places = Counter(_place_of(m["description"]) for m in cl["members"])
    top = [p for p, _ in places.most_common() if p != "Trip"][:3]
    onloc = [m["date"] for m in cl["members"]
             if (m.get("group_category") or m["category"]) != "Travel"
             and m["amount"] < 0]
    if not onloc:
        first = min(m["date"] for m in cl["members"])
        place = (" (" + " / ".join(top) + ")") if top else ""
        return "Booking" + place + " " + dt.date.fromisoformat(first).strftime("%b %Y")
    first = min(onloc)
    label = " / ".join(top) if top else "Trip"
    return label + " " + dt.date.fromisoformat(first).strftime("%b %Y")


def _auto_trip_id(members):
    """Content-derived id for an auto-detected trip: sha1 over its member ids.

    It used to be the cluster's POSITION ("trip%d" % i), which is not an identity —
    the index shifts whenever clustering changes (a new statement adds or merges a
    cluster, _consolidate_groups moves group siblings, a sub-2-member cluster is
    skipped after the index is taken, a trip is materialized out of auto-clustering).
    Anything keyed by trip id then re-attached to a DIFFERENT trip: notably the
    "Got back $" figure in rules/expected_reimb.json, which made the shortfall
    reconciliation compare one trip's marked-reimbursed total against another
    trip's received deposit. Hashing the membership means the id either identifies
    the same trip or stops existing (the amount visibly detaches) — never silently
    points at someone else's trip. Same scheme as a group id in app.py.
    """
    ids = sorted(str(m.get("id", "")) for m in members)
    return "trip" + hashlib.sha1(",".join(ids).encode()).hexdigest()[:12]


def cluster_trips(out):
    """
    Assemble trips in two passes and write data/trips.json:
      1. MANUAL trips (rules/manual_trips.json + rules/trip_members.json) — you
         create named trips and assign transactions by hand. These win and are
         removed from auto-clustering.
      2. AUTO trips — remaining trip-flagged txns clustered by date proximity
         (see _auto_cluster), named by dominant location.
    Reconciliation groups are kept intact throughout so a group's reimbursements
    always net against the same trip as its charge. A single auto-flagged txn is
    NOT inferred into its own trip — it stays trip=True with no trip_id and
    surfaces in the Trips "to place" staging tray, avoiding phantom 1-txn trips.
    """
    gmembers, grp_of = _group_siblings(out)
    summary, assigned = _build_manual_trips(out, grp_of)

    # NOTE: advance bookings (a hotel/flight bought weeks before you travel) form
    # their own small cluster, separate from the on-location activity. We do NOT
    # auto-fold them into a nearby trip — the booking's description often lies
    # about location (Booking.com bills from Amsterdam regardless of the hotel),
    # so which trip it funds isn't reliably knowable. Merge it in by hand.
    clusters = _auto_cluster(out, assigned)
    _consolidate_groups(clusters, gmembers, assigned)

    for cl in clusters:
        # A real trip is a CLUSTER (>=2 date-proximate txns), not a lone charge.
        if len(cl["members"]) < 2:
            continue
        summary.append(_trip_summary(_auto_trip_id(cl["members"]),
                                     _name_cluster(cl), cl["members"]))

    # attach the reimbursement each trip was actually paid back (for reconciling
    # against what's marked reimbursed in the UI).
    expected = load_json(RULES / "expected_reimb.json", {})
    for s in summary:
        amt = expected.get(s["id"])
        if amt:
            s["expected_reimbursement"] = amt

    summary.sort(key=lambda s: -s["spend"])
    write_json(DATA / "trips.json", summary)
    return summary


def main():
    txns = load_json(DATA / "transactions.json", [])
    rules_doc = load_json(RULES / "categories.json", {"rules": []})
    overrides = load_json(RULES / "overrides.json", {})
    txn_overrides = load_json(RULES / "txn_overrides.json", {})
    compiled = build_matcher(rules_doc)
    settings = load_json(RULES / "settings.json", {})
    configured_home_state = settings.get("home_state")
    home_state = (
        str(configured_home_state).strip().upper()
        if configured_home_state not in (None, "")
        else None
    )
    if home_state is not None and home_state not in _US_STATES:
        raise ValueError(
            "rules/settings.json home_state must be a two-letter US state or DC"
        )
    fallbacks = configured_fallbacks(settings)

    trip_flags = load_json(RULES / "trip_flags.json", {})   # id -> bool (user overrides)
    reimbursed = set(str(x) for x in load_json(RULES / "reimbursed.json", []))  # expense ids paid back
    reviewed = set(str(x) for x in load_json(RULES / "reviewed.json", []))  # low-conf txns accepted as-is

    # Per-row categorization cache — the hot path (~70% of a recat is matching every
    # rule needle against every txn). categorize_one is a PURE function of the txn's
    # (description, source, account behavior, amount) plus the rules + merchant
    # overrides, so its
    # result is safely cacheable. The cache is versioned by a hash of exactly those
    # rule inputs: change categories.json or overrides.json and the version differs,
    # invalidating the WHOLE cache (no stale categories possible). Per-id
    # txn_overrides / reviewed are applied OUTSIDE categorize_one (below), so caching
    # its output can't interact with them. Unchanged rows on a repeat recat hit the
    # cache; only new/changed descriptions recompute.
    cache_ver = hashlib.sha256(
        (json.dumps(rules_doc, sort_keys=True) + "\0"
         + json.dumps(overrides, sort_keys=True) + "\0"
         + json.dumps(fallbacks, sort_keys=True))
        .encode()).hexdigest()
    _cache = load_json(DATA / "catcache.json", {})
    cache = _cache.get("entries", {}) if _cache.get("version") == cache_ver else {}
    new_cache = {}
    def _ckey(t):
        return (
            f'{t["description"]}\x1f{t["source"]}\x1f'
            f'{t.get("account_type", "")}\x1f{t["amount"]}'
        )

    out = []
    for t in txns:
        t = dict(t)
        mkey = merchant_key(t["description"])
        if str(t.get("id")) in txn_overrides:
            # A single-transaction override (set in the UI) wins over everything.
            cat, conf, flag = txn_overrides[str(t["id"])], "user", False
        else:
            ck = _ckey(t)
            hit = cache.get(ck)
            if hit is not None:
                cat, conf, flag = hit[0], hit[1], hit[2]
            else:
                cat, conf, flag = categorize_one(
                    t, compiled, overrides, mkey, fallbacks
                )
            new_cache[ck] = [cat, conf, flag]      # rebuild cache from THIS run's rows
        t["category"] = cat
        t["confidence"] = conf      # user | rule | low
        # a low-confidence guess the user explicitly ACCEPTED as-is clears the
        # review flag without needing a category change (rules/reviewed.json).
        t["needs_review"] = flag and str(t.get("id")) not in reviewed
        t["merchant_key"] = mkey
        # 2-D trip tag: keep the true category, mark whether it happened on a
        # trip. Auto by location outside the configured home state or at a
        # recognized foreign destination, overridable per transaction.
        # Never auto-flag a non-spend txn (a Robinhood/Fidelity transfer that
        # happens to geolocate out-of-state isn't trip spend).
        auto_trip = (home_state is not None
                     and t["amount"] < 0 and cat not in NON_SPEND
                     and is_trip_location(t["description"], home_state))
        tid = str(t.get("id"))
        t["trip"] = trip_flags[tid] if tid in trip_flags else auto_trip
        # A NON_SPEND txn (Income/Transfer/Savings) is never trip spend, even if
        # its description geolocates out-of-state (a Robinhood transfer isn't a
        # trip) — guards test_nonspend_never_trip_flagged and clears stale flags.
        if cat in NON_SPEND:
            t["trip"] = False
        # Per-expense WORK REIMBURSEMENT flag: you mark which specific trip
        # expenses were paid back (rules/reimbursed.json). Reimbursed expenses keep
        # their category in the trip's gross bars but cancel out of Overview spend.
        t["reimbursed"] = tid in reimbursed
        # Record WHETHER this flag came from location auto-detection or from the
        # user's manual ✈. Only auto-detected flags feed auto-CLUSTERING; a
        # manual flag goes to the staging tray (trip=True, no trip_id) until you
        # place it — so hand-flagging two home charges never spawns a phantom
        # trip. (Assigning to a manual trip via trip_members overrides this.)
        t["trip_auto"] = bool(auto_trip)
        out.append(t)

    out = apply_manual_layers(out)
    cluster_trips(out)

    # Description overrides (user-friendly labels) — applied LAST, purely for
    # display. Categorization, merchant_key, trip-location detection and trip
    # naming all ran above on the ORIGINAL text, so relabeling never silently
    # recategorizes a txn or breaks a merchant rule. The original is preserved
    # as raw_description. Feeds every tab (all render from categorized.json).
    desc_ov = load_json(RULES / "desc_overrides.json", {})
    if desc_ov:
        for t in out:
            new = desc_ov.get(str(t.get("id")))
            if new:
                t["raw_description"] = t["description"]
                t["description"] = new
    write_json(DATA / "categorized.json", out)
    # small run-summary the dashboard can surface (e.g. pre-entries reconciled)
    write_json(DATA / "meta.json", {
        "manual_reconciled": MANUAL_RECONCILED,
        "home_state": home_state,
    })
    # Persist the per-row categorization cache (rebuilt from this run's rows, so
    # rows for deleted txns naturally drop out). Versioned by the rules hash above.
    write_json(DATA / "catcache.json", {"version": cache_ver, "entries": new_cache})

    # Summary
    cc = Counter(t["category"] for t in out)
    flagged = sum(1 for t in out if t["needs_review"])
    rec = f" {MANUAL_RECONCILED} pre-entered manual txns reconciled with statements." if MANUAL_RECONCILED else ""
    print(f"Categorized {len(out)} txns. {flagged} flagged for review.{rec}")
    for cat, n in cc.most_common():
        print(f"  {n:4d}  {cat}")


if __name__ == "__main__":
    main()
