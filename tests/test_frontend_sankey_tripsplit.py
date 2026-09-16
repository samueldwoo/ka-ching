"""Functional regression test for the Cash Flow (Sankey) trip/everyday split.

Round 4, defect 1 (correctness/major): renderSankey()'s trip-vs-everyday split
must be computed on the SAME effective, netted basis the Overview's Trip-vs-Home
view (catTotalsView with TRIPVIEW on) uses. The old code built tripCats only from
outflows (`tx.filter(t=>included(t)&&t.amount<0)`) and then derived
`homeTot=spendTotal-tripTot`. Because spendTotal (via catTotals) NETS trip-flagged
inflows (friend paybacks / refunds tagged to a trip) while tripCats excluded them
(amount<0), a trip-flagged inflow was silently subtracted from the HOME bucket
instead of the trip bucket — so the Cash Flow split disagreed with the Overview
Trip-vs-Home split for the identical month (a real $362.50 misallocation observed
for 2026-04), and could even drive homeTot negative.

Rather than assert on source text, this EXTRACTS the real helper functions and the
split fragment from index.html and EXECUTES them in node against a synthetic ledger
containing a trip-flagged inflow, then asserts the Sankey split (tripTot/homeTot)
reconciles exactly with catTotalsView's trip/home partition. Skips if node absent."""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML_PATH = BASE / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")


def _extract_fn(name, html=HTML):
    start = html.index("function " + name + "(")
    j = html.index("{", start)
    depth = 0
    while j < len(html):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    return html[start:j]


def _extract_split_fragment():
    """The tripCats/homeCats/tripTot/homeTot block inside renderSankey()."""
    m = re.search(
        r"const tripCats=\{\}, homeCats=\{\};.*?const homeTot=[^;]*;",
        HTML,
        re.S,
    )
    assert m, "trip/home split fragment not found in renderSankey()"
    return m.group(0)


HARNESS = textwrap.dedent(r"""
    const helpers=process.argv[3];
    const fragment=process.argv[4];
    const run=new Function('DATA','EXCLUDED','NONSPEND','TRIPVIEW','SANKEYTRIP','tx',
      helpers + `
      ;const cats={}; tx.filter(isSpend).forEach(t=>{const c=t.group_category||t.category;cats[c]=(cats[c]||0)+spendAmt(t);});
      const spendTotal=Object.entries(cats).filter(([c])=>!EXCLUDED.has(c)).reduce((s,[,v])=>s+v,0);
      ` + fragment + `
      ;const view=catTotalsView(tx);
      let viewTrip=0, viewHome=0;
      for(const k of Object.keys(view)){
        if(k.endsWith(' (trip)')) viewTrip+=view[k];
        else if(k.endsWith(' (home)')) viewHome+=view[k];
      }
      return {tripTot,homeTot,viewTrip,viewHome,spendTotal};
    `);
    const DATA=JSON.parse(process.argv[2]);
    const out=run(DATA, new Set(), new Set(), true, true, DATA.transactions);
    console.log(JSON.stringify(out));
""")


def test_sankey_tripsplit_nets_trip_inflow_into_trip_bucket(tmp_path):
    assert shutil.which("node"), "Node.js is required for frontend behavioral tests"
    helpers = "\n".join(
        _extract_fn(n)
        for n in ("isReimbursed", "isSpend", "spendAmt", "included",
                  "catTotals", "catTotalsView")
    )
    fragment = _extract_split_fragment()
    data = {"transactions": [
        # a genuine trip charge
        {"id": 1, "date": "2026-04-05", "amount": -2000.0, "category": "Travel",
         "trip": True, "description": "hotel"},
        # a trip-flagged INFLOW (friend paying back a trip cost) — the crux.
        {"id": 2, "date": "2026-04-06", "amount": 362.50, "category": "Travel",
         "trip": True, "description": "Zelle Payment From Andrea Ho hotel"},
        # ordinary everyday (home) spend
        {"id": 3, "date": "2026-04-10", "amount": -541.52, "category": "Dining",
         "trip": False, "description": "dinner"},
    ]}
    harness = tmp_path / "h.js"
    harness.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run(
        ["node", str(harness), json.dumps(data), helpers, fragment],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(r.stdout)
    # The Sankey split must equal the Overview Trip-vs-Home partition exactly.
    assert round(out["tripTot"], 2) == round(out["viewTrip"], 2), (
        "Sankey trip total must match the Overview Trip-vs-Home trip side "
        f"(netting the trip-flagged inflow). Got {out}"
    )
    assert round(out["homeTot"], 2) == round(out["viewHome"], 2), (
        "Sankey everyday total must match the Overview home side; the trip inflow "
        f"must not be subtracted from home. Got {out}"
    )
    # Concretely: trip = 2000 - 362.50 = 1637.50, home = 541.52.
    assert round(out["tripTot"], 2) == 1637.50, out
    assert round(out["homeTot"], 2) == 541.52, out
    # And the grand total still reconciles with spendTotal (conservation).
    assert round(out["tripTot"] + out["homeTot"], 2) == round(out["spendTotal"], 2), out
