"""Functional regression test for months() (index.html).

Round 3, defect 1 (correctness/major): a reconciliation group pinned to a
report_month past the newest raw statement carries its net only on its synthetic
agg_date-based EFFECTIVE entry. The default (non-smooth) monthly views iterate
months(); if months() derives its universe from raw member dates alone, that
month is never iterated and the group's entire net spend disappears from every
monthly view — while the server keys budgets/invest purely by agg_date.

Rather than assert on source text, this test EXTRACTS the real buildEffective()
and months() from index.html and EXECUTES them in node against a synthetic ledger
whose only 2026-09 spend lives on a group pinned ahead of the last statement
(2026-07). The old (buggy) months() returns just ['2026-07']; the fix returns
['2026-07','2026-09']. Skips cleanly if node is unavailable."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = BASE / "index.html"

HARNESS = textwrap.dedent(r"""
    const fs=require('fs');
    const html=fs.readFileSync(process.argv[2],'utf8');
    function extract(name){
      const start=html.indexOf('function '+name+'(');
      if(start<0) throw new Error('not found: '+name);
      let j=html.indexOf('{',start), depth=0;
      for(;j<html.length;j++){const c=html[j];
        if(c==='{')depth++;else if(c==='}'){depth--;if(depth===0){j++;break;}}}
      return html.slice(start,j);
    }
    // buildEffective() now delegates the month/index union to buildIndexes();
    // extract both so months() reads the prebuilt list (behavioral, not textual).
    const src=[extract('addMonths'),extract('buildEffective'),extract('buildIndexes'),extract('months')].join('\n');
    // SMOOTH stays false (default view) — the branch where the bug lived.
    const run=new Function('DATA','SMOOTH', src+`
      ;let EFFECTIVE=[], _byMonthEff={}, _monthsList=[], _gidColor={};
      buildEffective();
      return {months:months()};`);
    const DATA={transactions:[
      {id:1,date:'2026-07-10',amount:-40,category:'Dining',description:'coffee'},
      // reconciliation group pinned ahead to 2026-09 (flights booked months early);
      // no raw member txn falls in 2026-09 — its net lives only on agg_date.
      {id:2,date:'2026-07-15',amount:-500,category:'Travel',group_id:'g1',
       group_category:'Travel',agg_date:'2026-09-01',description:'flight',
       group_name:'Sep trip'},
    ]};
    const {months}=run(DATA,false);
    console.log(JSON.stringify({months}));
""")


def test_months_includes_group_pinned_past_last_statement(tmp_path):
    assert shutil.which("node"), "Node.js is required for frontend behavioral tests"
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(HTML)],
        capture_output=True, text=True, check=True,
    )
    months = json.loads(out.stdout)["months"]
    # the pinned-ahead group's month must be present in the (non-smooth) universe
    # so its net spend is actually iterated by every monthly view.
    assert "2026-09" in months, (
        "months() dropped the pinned-ahead group's month in the default view "
        f"-> its spend vanishes. Got: {months}"
    )
    # sanity: the raw statement month is still there too.
    assert "2026-07" in months


# addMonths() feeds the Smooth lens (amortization slices), months(), the category
# drill-down and the group reporting-month picker. Parsing "YYYY-MM-01T00:00" as
# LOCAL midnight and reading the month back out of toISOString() (UTC) shifts every
# result one month EARLY in any UTC+ zone, so this pins the arithmetic under a
# forced timezone instead of trusting whatever the test runner happens to be in.
TZ_HARNESS = textwrap.dedent(r"""
    const fs=require('fs');
    const html=fs.readFileSync(process.argv[2],'utf8');
    const start=html.indexOf('function addMonths(');
    let j=html.indexOf('{',start), depth=0;
    for(;j<html.length;j++){const c=html[j];
      if(c==='{')depth++;else if(c==='}'){depth--;if(depth===0){j++;break;}}}
    const addMonths=new Function(html.slice(start,j)+';return addMonths;')();
    console.log(JSON.stringify({
      same:addMonths('2026-03',0), next:addMonths('2026-03',1),
      wrap:addMonths('2026-12',1), back:addMonths('2026-01',-1),
      span:Array.from({length:12},(_,i)=>addMonths('2026-03',i)),
      dst:addMonths('2026-03',1),
    }));
""")


def _run_addmonths(tmp_path, tz):
    harness = tmp_path / f"tz_{tz.replace('/', '_')}.js"
    harness.write_text(TZ_HARNESS, encoding="utf-8")
    import os
    env = dict(os.environ, TZ=tz)
    out = subprocess.run(["node", str(harness), str(HTML)],
                         capture_output=True, text=True, check=True, env=env)
    return json.loads(out.stdout)


def test_add_months_is_timezone_independent(tmp_path):
    """Same answers in a UTC+ zone, a UTC- zone, and one with a March DST shift."""
    assert shutil.which("node"), "Node.js is required for frontend behavioral tests"
    expected = {
        "same": "2026-03", "next": "2026-04", "wrap": "2027-01", "back": "2025-12",
        "span": ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08",
                 "2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02"],
        "dst": "2026-04",
    }
    for tz in ("Asia/Tokyo", "Europe/London", "Europe/Paris",
               "America/Los_Angeles", "UTC"):
        assert _run_addmonths(tmp_path, tz) == expected, \
            f"addMonths() must not depend on the local timezone (TZ={tz})"
