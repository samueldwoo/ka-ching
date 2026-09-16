"""Static regression checks that the api() choke-point retains its in-flight
mutation dedup and the mutate() success helper. These guard two recurring bug
CLASSES that were fixed at the choke point rather than per-call-site:

  1. Double-fire — a rapid double-click issuing the SAME POST twice (duplicate
     manual txns, racing /api/refresh runs). api() coalesces identical in-flight
     requests into one shared promise.
  2. False-success-then-revert — optimistic repaint + success toast even when the
     server returned {error} / {ok:false}. mutate() is the sanctioned helper that
     checks the server result before claiming success.

The dedup is JS-only, so there is no server round-trip to exercise from pytest;
these assert the shipped structure as text (same approach as the other
test_frontend_*.py suites), so reverting the fix fails the suite."""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = (BASE / "index.html").read_text(encoding="utf-8")


def _api_body():
    """Return the source of the api() function (through its closing brace)."""
    m = re.search(r"async function api\(path,body\)\{(.*?)\n\}", HTML, re.S)
    assert m, "api(path,body) function not found"
    return m.group(1)


def test_api_dedups_inflight_mutations():
    body = _api_body()
    # keyed by path + body so DISTINCT actions are never blocked...
    assert re.search(r"const key=path\+.*JSON\.stringify\(body", body), \
        "api() must key in-flight requests by path + serialized body"
    # ...an identical in-flight request returns the SAME promise (no 2nd fetch)...
    assert "_apiInflight.get(key)" in body and "return existing" in body, \
        "api() must return the existing in-flight promise for a duplicate call"
    # ...and the key is cleared when the promise settles so later repeats work.
    assert re.search(r"\.finally\(\(\)=>\{_apiInflight\.delete\(key\)", body), \
        "api() must clear the in-flight key on settle"
    # backward-compat: still resolves to parsed JSON, still one fetch per key.
    assert "r.json()" in body
    assert body.count("fetch(path") == 1, \
        "api() must issue exactly one fetch per distinct request"


def test_inflight_map_is_module_level():
    """The dedup map must persist across calls (module scope), not be re-created
    inside api() where it could never see a concurrent duplicate."""
    assert re.search(r"const _apiInflight=new Map\(\);\s*async function api\(",
                      HTML), "_apiInflight map must be declared just above api()"


def test_mutate_helper_checks_server_result():
    m = re.search(r"async function mutate\(path,body\)\{(.*?)\n\}", HTML, re.S)
    assert m, "mutate(path,body) helper not found"
    body = m.group(1)
    assert "await api(path,body)" in body, "mutate() must go through api()"
    # treats falsy body, {error} and {ok:false} as failure...
    assert "r.error" in body and "r.ok===false" in body and "!r" in body
    # ...surfaces a ⚠ toast on failure and returns {ok:false}...
    assert re.search(r"toast\('⚠ '\+", body), \
        "mutate() must show a warning toast on failure"
    assert "return {ok:false,r}" in body
    # ...and returns {ok:true,r} on success.
    assert "return {ok:true,r}" in body


def test_mutate_is_actually_wired_in():
    """A source-presence test on mutate() passed while mutate() had ZERO callers,
    so the false-success class was still live. Guard against that regression: the
    helper must have real adopters, not just exist."""
    callers = len(re.findall(r"await mutate\(", HTML))
    assert callers >= 20, (
        f"mutate() has only {callers} call sites — the false-success fix is not "
        "wired into the mutation handlers (it existed but was dead code before)."
    )


def test_key_mutation_handlers_use_mutate_not_bare_api():
    """The handlers that previously toasted success unconditionally must now route
    through mutate() so a server rejection suppresses the success toast. These are
    the specific fire-and-forget sites the audit loop kept re-finding."""
    for fn, endpoint in [
        ("setAllReimbursed", "/api/reimburse_bulk"),
        ("setGroupTrip", "/api/group_save"),
        ("ungroup", "/api/group_delete"),
        ("acceptReview", "/api/review_accept"),
        ("materializeTrip", "/api/trip_create"),
    ]:
        # find the endpoint call and assert it's a mutate(), not a bare api()
        assert re.search(r"mutate\('" + re.escape(endpoint), HTML), \
            f"{endpoint} (used by {fn}) must go through mutate(), not bare api()"
