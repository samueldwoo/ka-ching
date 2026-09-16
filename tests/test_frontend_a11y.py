"""Static regression checks on index.html for accessibility / UX defects that
have no server round-trip. These parse the shipped markup+CSS as text and assert
the specific structure each fix introduced, so a regression (reverting the fix)
fails the suite without needing a live browser."""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HTML = (BASE / "index.html").read_text(encoding="utf-8")


def test_reduced_motion_keeps_reveal_visible():
    """prefers-reduced-motion must not strand .reveal content at opacity:0.
    Disabling the 'rise' animation without restoring opacity leaves the Overview
    cards (which are .card AND .reveal) permanently blank."""
    m = re.search(r"@media\(prefers-reduced-motion:reduce\)\{([^}]*\}[^@]*)\}",
                  HTML)
    assert m, "reduced-motion media query not found"
    block = m.group(0)
    # the animation is disabled for .reveal too...
    assert ".reveal" in block and "animation:none" in block
    # ...AND opacity is restored so the content is actually visible.
    assert re.search(r"\.reveal\{[^}]*opacity:1", block), \
        "reduced-motion must reset .reveal opacity to 1"


def test_invest_tier_cards_are_keyboard_operable():
    """The three auto-invest tier cards are custom controls; the global
    Enter/Space shim only fires for role=button + tabindex=0."""
    m = re.search(r'<div class="invtier[^"]*"[^>]*>', HTML)
    assert m, "invtier card markup not found"
    tag = m.group(0)
    assert 'role="button"' in tag
    assert 'tabindex="0"' in tag
    assert "aria-pressed=" in tag


def test_takehome_editor_is_keyboard_operable():
    """The take-home inline editor (#payWrap) must be reachable/activatable by
    keyboard like every other custom control."""
    m = re.search(r'<span id="payWrap"[^>]*>', HTML)
    assert m, "#payWrap markup not found"
    tag = m.group(0)
    assert 'role="button"' in tag and 'tabindex="0"' in tag


def test_sortable_headers_keep_table_semantics():
    """Sortable columns need a real button inside a table header. Assigning
    role=button directly to <th> removes its useful column-header semantics."""
    headers = re.findall(r'<th(?=[^>]*scope="col")(?=[^>]*data-sort-column="[^"]+")[^>]*>(.*?)</th>',
                         HTML)
    assert len(headers) == 5, "transaction table should expose five sortable column headers"
    assert all(re.search(r'<button type="button" class="tsort" data-sort="[^"]+">', header)
               for header in headers), "each sortable column needs a native button"
    assert ".tsort{padding:0;border:0;background:none" in HTML


def test_budget_inputs_have_category_specific_names():
    """Each generated budget input needs an accessible label; visual table
    proximity alone is not reliably announced as its name."""
    assert 'aria-label="Monthly budget for ${esc(c)}"' in HTML


def test_category_drill_controls_report_expanded_state():
    assert 'aria-controls="catDrill" aria-expanded="false"' in HTML
    assert "function syncCatDrillControls()" in HTML


def test_trip_category_filters_are_keyboard_operable():
    assert '<div class="tripbar" role="button" tabindex="0" aria-pressed="' in HTML
    assert "tripbar').forEach(x=>x.onclick" in HTML
    assert "min-width:8px" in HTML
    assert ".tripchip{min-height:32px}" in HTML


def test_custom_trip_order_uses_the_left_drag_handle_only():
    assert "const lead=drag" in HTML
    assert 'class="draghandle"' in HTML
    assert ">⠿</span>" in HTML
    assert "tripmoveorder" not in HTML
    assert 'draggable="true"' in HTML


def test_invtier_and_paywrap_have_focus_style():
    assert re.search(r"\.invtier:focus-visible[^{]*\{[^}]*outline", HTML) or \
        re.search(r"\.invtier:focus-visible,#payWrap:focus-visible\{[^}]*outline",
                  HTML), "no focus-visible outline for invtier/payWrap"


def test_takehome_toast_reflects_actual_override():
    """The take-home inline editor's confirmation toast must branch on whether the
    override ACTUALLY took effect, not merely on the input being non-empty. The
    backend only honors a take-home ABOVE the ledger-detected check (check_overridden
    is true only when ov_check > detected_check + 1); a value at/below detected pay
    is stored but inert. The old code toasted 'Take-home updated' for any non-empty
    input — false success. After reloading INVEST, the save handler must consult the
    fresh INVEST.check_overridden and warn when the entry was a no-op."""
    # locate the take-home editor's save() closure.
    m = re.search(r"const save=async commit=>\{(.*?)\n    \};", HTML, re.S)
    assert m, "take-home save() closure not found"
    body = m.group(1)
    # INVEST must be reloaded (awaited) before the toast is chosen, so the branch
    # reads the fresh override state rather than the stale cached one.
    assert "await renderInvest()" in body, \
        "save() must await the INVEST reload before deciding the toast"
    # the success toast must be gated on the actual post-reload override flag,
    # NOT on inp.value being non-empty.
    assert "INVEST.check_overridden" in body, \
        "toast must branch on the reloaded INVEST.check_overridden"
    assert not re.search(r"inp\.value\.trim\(\)\?'Take-home updated'", body), \
        "toast must not claim 'Take-home updated' merely because input is non-empty"
    # a non-raise entry gets an explanatory warning rather than a false confirmation.
    assert "at or below your statement pay" in body, \
        "an at/below-detected entry must warn instead of confirming"


def test_cdgtoggle_group_toggle_is_keyboard_operable():
    """Category-drill group toggle must match the app's other group toggles."""
    m = re.search(r'<span class="cdgtoggle"[^>]*>', HTML)
    assert m, "cdgtoggle markup not found"
    tag = m.group(0)
    assert 'role="button"' in tag
    assert 'tabindex="0"' in tag
    assert "aria-expanded=" in tag


def test_transactions_sticky_thead_clears_app_header():
    """The sticky transactions thead must park below the sticky app header, not
    at top:0 where the higher-z-index header would occlude the column labels."""
    m = re.search(r"#tab-transactions thead th\{([^}]*)\}", HTML)
    assert m, "sticky thead rule not found"
    rule = m.group(1)
    assert "position:sticky" in rule
    # top must not be 0 — either an explicit offset or the shared --headerH var.
    top = re.search(r"top:([^;]+)", rule)
    assert top, "no top offset on sticky thead"
    val = top.group(1).strip()
    assert val not in ("0", "0px"), "sticky thead still parks at top:0"
    # if it uses the shared custom property, that property must be defined.
    if "--headerH" in val:
        assert re.search(r"--headerH:\s*\d+px", HTML), "--headerH not defined"


def test_multiselect_option_handlers_stop_propagation():
    """Round 2, defect 1: the multi-select filter option (.msopt) and clear
    (.msclear) onclick handlers must call e.stopPropagation(), otherwise the
    click bubbles to the document-level closeMS listener and the popover snaps
    shut after every single selection — defeating the OR-several-values intent.
    The parallel single-select .ddopt handler already does this; the multi-select
    twin must match."""
    # find openMS's render() body: from the msclear assignment through msopt.
    m = re.search(
        r"menu\.querySelector\('\.msclear'\)\.onclick=(.*?)"
        r"menu\.querySelectorAll\('\.msopt'\)\.forEach\(el=>el\.onclick=(.*?)\}\);",
        HTML, re.S)
    assert m, "openMS msclear/msopt onclick handlers not found"
    msclear_head, msopt_body = m.group(1), m.group(2)
    # both handlers must take the event and call stopPropagation before mutating
    # the Set / re-rendering (which would otherwise bubble to closeMS).
    assert "e.stopPropagation()" in msclear_head, \
        ".msclear onclick must call e.stopPropagation()"
    assert "e.stopPropagation()" in msopt_body, \
        ".msopt onclick must call e.stopPropagation()"


def test_multiselect_menu_survives_full_render():
    """Round 4, defect 1: the category/account multi-select popovers are backed
    by a Set (not a live <select>), created with class 'ddmenu msmenu', cached on
    trigger._msmenu and appended to document.body. enhanceAllSelects() runs on
    every global render() and removes '.ddmenu' nodes whose ._sel is gone — which
    matched the msmenu (it has no _sel) and detached it, while trigger._msmenu
    still pointed at it. The next openMS() then skipped the re-append branch and
    painted a detached node, so the filters silently stopped opening after any
    recategorize / month-change / toggle. Two independent guards must hold:
      (a) the cleanup predicate must EXCLUDE '.msmenu', and
      (b) openMS must re-append the cached menu if it was detached."""
    # (a) the cleanup querySelectorAll must not match .msmenu.
    m = re.search(r"function enhanceAllSelects\(root\)\{(.*?)\n\}", HTML, re.S)
    assert m, "enhanceAllSelects() not found"
    body = m.group(1)
    cleanup = re.search(r"querySelectorAll\('([^']*\.ddmenu[^']*)'\)\.forEach\(m=>\{[^}]*_sel[^}]*m\.remove\(\)",
                        body)
    assert cleanup, "ddmenu cleanup pass not found in enhanceAllSelects"
    sel = cleanup.group(1)
    assert ":not(.msmenu)" in sel, \
        "enhanceAllSelects cleanup must exclude .msmenu (it has no ._sel and would be removed)"
    # (b) openMS must re-append the cached menu when it is detached before rendering.
    oms = re.search(r"function openMS\(trigger,items,set,allLabel\)\{(.*?)const render=", HTML, re.S)
    assert oms, "openMS() prelude not found"
    prelude = oms.group(1)
    assert re.search(r"isConnected", prelude) and re.search(r"appendChild\(menu\)", prelude), \
        "openMS must re-append the cached menu when !menu.isConnected"


def test_review_badge_is_keyboard_operable():
    """Round 2, defect 2: the 'N to review' badge is wired with an onclick, but
    the global Enter/Space shim only fires for role=button + tabindex=0 elements.
    Without those attributes the badge is mouse-only — unreachable by keyboard/AT.
    Match every other clickable span in the app."""
    m = re.search(r'<span class="revbadge"[^>]*>', HTML)
    assert m, "revbadge markup not found"
    tag = m.group(0)
    assert 'role="button"' in tag, "revbadge missing role=button"
    assert 'tabindex="0"' in tag, "revbadge missing tabindex=0"
    assert "aria-label=" in tag, "revbadge missing aria-label"


def test_refresh_checks_pipeline_ok():
    """Round 2, defect 3: the Refresh handler must inspect the {ok} the backend
    returns and NOT show a success toast when the parse/categorize pipeline
    failed. A blind success toast would leave the user trusting stale data."""
    m = re.search(r"async function doRefresh\(b\)\{(.*?)\n\}", HTML, re.S)
    assert m, "doRefresh() handler not found"
    body = m.group(1)
    # must capture the response and branch on its .ok before the success toast.
    assert re.search(r"=\s*await api\('/api/refresh'", body), \
        "refresh must capture the api() result, not discard it"
    assert re.search(r"!r\.ok|!r\b|r\.ok", body), \
        "refresh must branch on the returned {ok}"
    assert "Refresh failed" in body, "no failure toast on a failed refresh"
    # the failure branch must early-return (guard the success toast).
    assert "return;" in body, "failure branch must early-return"


def test_load_handles_fetch_failure():
    """Round 2, defect 4: load() must check the response status and there must be
    an error card + Retry affordance, plus a .catch on the boot load() call, so a
    server/API error surfaces instead of freezing the UI on placeholder markup."""
    m = re.search(r"async function load\(\)\{(.*?)\n\}", HTML, re.S)
    assert m, "load() not found"
    body = m.group(1)
    assert re.search(r"if\(!r\.ok\)", body), \
        "load() must check the /api/data response status (r.ok)"
    # a dedicated error-card renderer must exist and offer a Retry.
    assert "function renderLoadError(" in HTML, "no renderLoadError() card"
    err = re.search(r"function renderLoadError\(\)\{(.*?)\n\}", HTML, re.S)
    assert err and "Retry" in err.group(1), "error card lacks a Retry button"
    # boot load() must have a .catch so an initial-load rejection is handled.
    assert re.search(r"load\(\)\.then\(.*?\)\.catch\(", HTML, re.S), \
        "boot load() missing a .catch handler"


def test_mutations_check_error_before_success_toast():
    """Round 2, defect 4 (mutations): budget-save, recategorize, and reimburse
    handlers must inspect the returned {error} before toasting success, matching
    #addSave / #groupSave. Otherwise a server-side {error} still shows success."""
    # Each success path uses mutate(), which normalizes HTTP/transport/API
    # failures to {ok:false} and emits the urgent error toast before callers can
    # show a success toast.
    bud = re.search(r"await mutate\('/api/budget'.*?toast\('Budget saved'\)", HTML, re.S)
    assert bud, "budget handler shape changed"
    assert "if(!ok)return" in bud.group(0), "budget save must stop on failure"
    # recategorize
    recat = re.search(r"await mutate\('/api/txn_category'.*?toast\('Recategorized'\)",
                      HTML, re.S)
    assert recat, "recat handler shape changed"
    assert "if(!ok)return" in recat.group(0), "recategorize must stop on failure"
    # reimburse
    reimb = re.search(r"await mutate\('/api/reimburse'.*?toast\(reimb\?", HTML, re.S)
    assert reimb, "reimburse handler shape changed"
    assert "if(!ok) return" in reimb.group(0), "reimburse must stop on failure"


def test_staging_bulk_actions_are_keyboard_operable():
    """Round 3, defect 1: the staging-tray bulk-action affordances ('drop all'
    and 'clear') are custom clickable spans wired click-only. The global
    Enter/Space shim only fires for role=button + tabindex=0, so without those
    attributes they are mouse-only — matching the sibling .stagedrop pattern."""
    for span_id in ("stageBulkDrop", "stageSelNone"):
        m = re.search(r'<span class="stagelink" id="%s"[^>]*>' % span_id, HTML)
        assert m, "%s markup not found" % span_id
        tag = m.group(0)
        assert 'role="button"' in tag, "%s missing role=button" % span_id
        assert 'tabindex="0"' in tag, "%s missing tabindex=0" % span_id
        assert "aria-label=" in tag, "%s missing aria-label" % span_id
    # and a visible focus indicator for keyboard users.
    assert re.search(r"\.stagelink:focus-visible\{[^}]*outline", HTML), \
        "no focus-visible outline for .stagelink"


def test_trip_clear_filter_is_keyboard_operable():
    """Round 3, defect 2: the trip 'clear filter' link is a custom clickable
    span wired click-only. The adjacent .tripchip/.tripbar toggles carry
    role=button + tabindex=0; the clear-filter affordance must match so the
    global Enter/Space shim reaches it."""
    m = re.search(r'<span class="tripclearfilt"[^>]*>', HTML)
    assert m, "tripclearfilt markup not found"
    tag = m.group(0)
    assert 'role="button"' in tag, "tripclearfilt missing role=button"
    assert 'tabindex="0"' in tag, "tripclearfilt missing tabindex=0"
    assert "aria-label=" in tag, "tripclearfilt missing aria-label"
    assert re.search(r"\.tripclearfilt:focus-visible\{[^}]*outline", HTML), \
        "no focus-visible outline for .tripclearfilt"


def test_headerH_is_measured_not_hardcoded():
    """Round 3, defect 3: the sticky transactions thead parks at
    top:var(--headerH). The app header uses flex-wrap and grows past the 63px
    default when it wraps (mobile/narrow, or when the Modeling badge shows), so
    --headerH must be recomputed from the header's real height after layout —
    otherwise column labels float behind the taller header. Assert a
    ResizeObserver (or resize fallback) writes offsetHeight into --headerH."""
    # the sticky rule must still reference the shared variable.
    m = re.search(r"#tab-transactions thead th\{([^}]*)\}", HTML)
    assert m and "top:var(--headerH)" in m.group(1), \
        "sticky thead no longer uses var(--headerH)"
    # something must actually set --headerH at runtime (no setProperty == bug).
    assert re.search(
        r"setProperty\(\s*'--headerH'\s*,[^)]*offsetHeight", HTML), \
        "--headerH is never recomputed from the header's real offsetHeight"
    # the measurement must be driven by layout changes, not a one-shot read.
    assert "ResizeObserver" in HTML, \
        "header height not tracked via ResizeObserver"


def test_rowmenu_is_keyboard_operable():
    """Re-audit round 1: the per-row actions menu (⋯) must be keyboard-operable
    like the app's other popovers (openDD/openMS). editDesc/openEditTxn/
    amortizeTxn/deleteTxn are reachable ONLY through this menu, so if it can't be
    driven by keyboard those actions are entirely keyboard-inaccessible.

    Four independent guards must hold:
      (a) the container is role="menu" and openRowMenu renders role="menuitem"
          buttons,
      (b) openRowMenu moves focus INTO the popover on open (m.querySelector
          ('button')?.focus() or equivalent),
      (c) a keydown handler on the menu supports Escape (close) and arrow-key
          roving focus,
      (d) the ⋯ trigger carries aria-expanded (toggled open/closed) and the
          Escape/close path returns focus to the trigger."""
    # (a) container role=menu + trigger aria wiring.
    cont = re.search(r'<div id="rowMenu"[^>]*>', HTML)
    assert cont and 'role="menu"' in cont.group(0), \
        '#rowMenu container must have role="menu"'
    trg = re.search(r'<span class="rowact rowmenu"[^>]*>', HTML)
    assert trg, "rowmenu trigger markup not found"
    assert 'aria-expanded=' in trg.group(0), \
        "rowmenu trigger must carry aria-expanded"

    # isolate the openRowMenu body.
    m = re.search(r"function openRowMenu\(id,e\)\{(.*?)\n\}", HTML, re.S)
    assert m, "openRowMenu() not found"
    body = m.group(1)
    # menu items rendered as role=menuitem buttons.
    assert 'role="menuitem"' in body, \
        "openRowMenu must render items as role=menuitem"
    # (b) focus is moved into the popover on open.
    assert re.search(r"querySelector\('button'\)\??\.?\.?focus\(\)", body) or \
        re.search(r"querySelector\('button'\)\?\.focus\(\)", body), \
        "openRowMenu must move focus into the menu on open"
    # aria-expanded set to true on the trigger when opening.
    assert re.search(r"setAttribute\('aria-expanded','true'\)", body), \
        "openRowMenu must set aria-expanded=true on the trigger"
    # (c) a keydown handler with Escape + arrow roving focus.
    assert "onkeydown" in body, "openRowMenu registers no keydown handler"
    assert "ArrowDown" in body and "ArrowUp" in body, \
        "rowMenu keydown must support arrow-key roving focus"
    assert "Escape" in body, "rowMenu keydown must handle Escape"
    # (d) a close path that clears aria-expanded and can return focus to trigger.
    cm = re.search(r"function closeRowMenu\((.*?)\)\{(.*?)\n\}", HTML, re.S)
    assert cm, "closeRowMenu() not found"
    cbody = cm.group(2)
    assert re.search(r"setAttribute\('aria-expanded','false'\)", cbody), \
        "closeRowMenu must clear aria-expanded on the trigger"
    assert ".focus()" in cbody, \
        "closeRowMenu must be able to return focus to the triggering ⋯"
    # a document-level Escape listener must route to closeRowMenu.
    assert re.search(r"key==='Escape'\)closeRowMenu", HTML) or \
        re.search(r"Escape.*closeRowMenu", HTML), \
        "no document-level Escape wiring to closeRowMenu"



def test_toast_region_is_announced_to_screen_readers():
    """Re-audit round 2, defect 1: the #toast div is the app's ONLY feedback
    surface for every mutation and error ('Budget saved', 'Recategorized',
    '⚠ Refresh failed…', 'Only manual transactions can be edited', etc.). Without
    a live-region role a screen-reader user gets no announcement that a save
    succeeded, was rejected, or that a Refresh silently failed. The region must
    exist in the markup (so text inserted into it is announced) and errors/guards
    must escalate to an assertive announcement."""
    # (a) the toast region carries a live-region role in the STATIC markup, so it
    # exists before any textContent is inserted.
    m = re.search(r'<div class="toast" id="toast"[^>]*>', HTML)
    assert m, "#toast markup not found"
    tag = m.group(0)
    assert 'role="status"' in tag, "#toast must declare role=status"
    assert 'aria-live="polite"' in tag, "#toast must declare aria-live=polite"
    assert 'aria-atomic="true"' in tag, "#toast must declare aria-atomic=true"

    # (b) toast() must accept an urgency flag and set an assertive/alert role for
    # error+guard toasts so they interrupt rather than being missed.
    assert re.search(r"function toast\(msg,\s*urgent\)", HTML), \
        "toast() must accept an `urgent` flag for assertive error announcements"
    trole = re.search(r"function toastRole\(t,\s*urgent\)\{(.*?)\}", HTML, re.S)
    assert trole, "toastRole() helper not found"
    body = trole.group(1)
    assert "'alert'" in body and "'assertive'" in body, \
        "toastRole must escalate urgent toasts to role=alert / aria-live=assertive"
    assert "'status'" in body and "'polite'" in body, \
        "toastRole must fall back to role=status / aria-live=polite"

    # (c) mutate owns API failures and must surface them as urgent toasts.
    mutate = re.search(r"async function mutate\(path,body\)\{(.*?)\n\}", HTML, re.S)
    assert mutate and "toast('⚠ '" in mutate.group(1) and "true" in mutate.group(1), \
        "mutate() must announce API failures urgently"
    assert re.search(r"toast\('Only manual transactions can be edited',\s*true\)", HTML), \
        "the edit-guard toast must be marked urgent"
    assert re.search(r"toast\('⚠ Refresh failed[^']*',\s*true\)", HTML), \
        "the Refresh-failed toasts must be marked urgent"


def test_month_picker_trigger_is_keyboard_operable():
    """Re-audit round 2, defect 2: the custom month picker trigger (#monthLabel)
    opens the #monthPop grid but, unlike every sibling popover (openDD/openMS/
    openRowMenu), carried no aria-haspopup/aria-expanded and did no focus
    management. AT users got no open/close state and focus was stranded on the
    trigger. Mirror the established convention.

    Guards:
      (a) #monthLabel declares aria-haspopup + aria-expanded,
      (b) openMonthPop toggles aria-expanded=true and moves focus into a .mpcell,
      (c) closeMonthPop clears aria-expanded and returns focus to #monthLabel."""
    # (a) trigger aria wiring.
    m = re.search(r'<button id="monthLabel"[^>]*>', HTML)
    assert m, "#monthLabel markup not found"
    tag = m.group(0)
    assert "aria-haspopup=" in tag, "#monthLabel missing aria-haspopup"
    assert 'aria-expanded="false"' in tag, \
        "#monthLabel missing initial aria-expanded=false"

    # (b) openMonthPop toggles expanded + focuses a grid cell.
    op = re.search(r"function openMonthPop\(\)\{(.*?)\n\}", HTML, re.S)
    assert op, "openMonthPop() not found"
    ob = op.group(1)
    assert re.search(r"setAttribute\('aria-expanded','true'\)", ob), \
        "openMonthPop must set aria-expanded=true on the trigger"
    assert ".mpcell" in ob and ".focus()" in ob, \
        "openMonthPop must move focus into a .mpcell on open"

    # (c) closeMonthPop clears expanded + returns focus to the trigger.
    cp = re.search(r"function closeMonthPop\(\)\{(.*?)\n\}", HTML, re.S)
    assert cp, "closeMonthPop() not found"
    cb = cp.group(1)
    assert re.search(r"setAttribute\('aria-expanded','false'\)", cb), \
        "closeMonthPop must clear aria-expanded on the trigger"
    assert ".focus()" in cb, \
        "closeMonthPop must return focus to #monthLabel"


def test_chart_tooltips_are_clamped_to_viewport():
    """Re-audit round 2, defect 3: chart/donut/Sankey tooltips were positioned as
    left=clientX+14 with no viewport clamp on a position:fixed white-space:nowrap
    element, so a long tip near the right/top edge (e.g. the rightmost Sankey node,
    the last bar in a scrolled Trends chart) clipped off-screen — hiding the value
    it exists to reveal. Every other floating layer clamps with
    Math.min(..., window.innerWidth - w - 8). A shared placeTip() must do the same
    and be used by all four chart tooltip sites."""
    # a shared helper must exist and clamp both axes to the viewport.
    ph = re.search(r"function placeTip\(tip,\s*e\)\{(.*?)\n\}", HTML, re.S)
    assert ph, "placeTip() helper not found"
    body = ph.group(1)
    assert "getBoundingClientRect()" in body, \
        "placeTip must measure the tip (getBoundingClientRect) before clamping"
    assert re.search(r"Math\.min\([^)]*window\.innerWidth\s*-\s*r\.width", body), \
        "placeTip must clamp left to window.innerWidth - r.width"
    assert "window.innerHeight" in body, \
        "placeTip must clamp the top axis to the viewport height too"

    # the raw unclamped pattern must be gone everywhere...
    assert "tip.style.left=(e.clientX+14)" not in HTML, \
        "an unclamped chart tooltip (clientX+14) still remains"
    # ...and placeTip must be called from at least the three chart sites
    # (donut, wireTips bars, Sankey ribbons).
    assert HTML.count("placeTip(tip,e)") >= 3, \
        "placeTip must be wired into the donut, bar-chart, and Sankey tooltips"


def test_rail_nav_contains_its_own_overflow():
    """Navigation is a prominent VERTICAL left rail. The buttons stack in a column
    that scrolls VERTICALLY within the fixed-height rail (so the nav never pushes
    the page wide), collapse to icon-only on the collapsed rail or narrow
    viewports, and keep their labels un-wrapped. switchTab must still scroll the
    active tab into view within that scroll container."""
    rules = re.findall(r"\.tabs\{([^}]*)\}", HTML)
    assert rules, ".tabs rule not found"
    rule = next((r for r in rules if "flex-direction:column" in r), "")
    assert "flex-direction:column" in rule, \
        ".tabs must stack vertically in the rail (flex-direction:column)"
    assert "overflow-y:auto" in rule, \
        ".tabs must scroll vertically within the rail instead of overflowing"
    # thin/hidden scrollbar styling to match .chartscroll/.ddmenu
    assert re.search(r"\.tabs::-webkit-scrollbar\{", HTML), \
        ".tabs must style its scrollbar to match the other scroll regions"
    # buttons must not wrap their label text
    btn_rules = re.findall(r"\.tabs button\{([^}]*)\}", HTML)
    assert btn_rules, ".tabs button rule not found"
    assert any("white-space:nowrap" in rule for rule in btn_rules), \
        "tab labels must not wrap"
    # Labels collapse only for an explicitly collapsed desktop rail. Mobile
    # switches to a labeled bottom navigation, avoiding an ambiguous icon rail.
    assert re.search(r"\.rail-collapsed .tabs .tl[^{]*\{[^}]*display:none", HTML), \
        "collapsed rail must hide tab labels (icon-only)"
    assert re.search(r"@media\(max-width:760px\).*?\.tabs \.tl\{display:block", HTML, re.S), \
        "narrow viewports must expose labels in the bottom navigation"
    # switchTab must scroll the active tab into view within the scroll container
    st = re.search(r"function switchTab\(name\)\{(.*?)\n\}", HTML, re.S)
    assert st, "switchTab() not found"
    assert "scrollIntoView" in st.group(1), \
        "switchTab must scroll the active tab into view within the rail"


def test_account_filter_matches_displayed_account_not_source_code():
    """Re-audit round 4, defect 1: the Transactions Account filter must match the
    same field the Account COLUMN renders (t.account), NOT the raw parser source
    code (t.source). A manual txn always has source='manual' but carries a
    user-chosen account label (e.g. 'Sapphire Preferred'); filtering on t.source
    would hide it under the very label the column shows. Two guards:
      (a) the filter predicate matches on t.account, and
      (b) the option list is built from account LABELS present in the data
          (so a manual 'Sapphire Preferred' row is filterable under that label)."""
    # (a) the filter must test the displayed account, not the source code.
    assert re.search(r"TXN_SRCFILTER\.has\(t\.account\)", HTML), \
        "Account filter must match t.account (the displayed column), not t.source"
    assert "TXN_SRCFILTER.has(t.source)" not in HTML, \
        "Account filter must not match the raw parser source code (t.source)"
    # (b) msItems_src must derive options from the account labels in the data.
    m = re.search(r"function msItems_src\(\)\{(.*?)\n\}", HTML, re.S)
    assert m, "msItems_src() not found"
    body = m.group(1)
    assert "DATA" in body and "t.account" in body, \
        "msItems_src must build options from t.account labels present in the data"
    # the hard-coded source-code option list ([['csp',...],['vx',...]]) must be gone.
    assert "['csp'," not in HTML and "['vx'," not in HTML, \
        "msItems_src must not hard-code parser source codes as filter values"


def test_arrow_month_stepper_guards_open_popovers():
    """Re-audit round 4, defect 2: the global ←/→ month-stepper must early-return
    while a themed popover owns keyboard focus (custom month picker, or the
    single/multi-select menus). Their option rows/cells are focusable buttons/divs
    (not input/select/textarea), so without the guard Left/Right leaks to the
    stepper and silently changes the month behind the open control. Mirror the
    ddOpenMenu guard the Tab focus-trap already uses."""
    # isolate the arrow-shortcut handler: the keydown listener that steps the
    # month — from the ArrowLeft check back to the start of its addEventListener.
    m = re.search(r"document\.addEventListener\('keydown',e=>\{((?:(?!addEventListener).)*?stepMonth\(-1\).*?)\n\}\);",
                  HTML, re.S)
    assert m, "arrow month-stepper handler not found"
    body = m.group(1)
    # a single early-return guard must cover all three themed popovers and sit
    # BEFORE the stepMonth calls (so the arrow keys never reach the stepper).
    pre = body.split("stepMonth")[0]
    guard = re.search(r"if\([^\n]*\)\s*return;\s*\n\s*if\(e\.key==='ArrowLeft'", body)
    assert "ddOpenMenu" in pre, "guard must check the single-select menu (ddOpenMenu)"
    assert "msOpenMenu" in pre, "guard must check the multi-select menu (msOpenMenu)"
    assert "monthPop" in pre and "'open'" in pre, \
        "guard must check the open month picker (#monthPop .open)"
    # and the popover guard must early-return (appear as a `... return;` line).
    assert re.search(r"ddOpenMenu\|\|msOpenMenu\|\|\$\('#monthPop'\)\.classList\.contains\('open'\)\)\s*return;", pre), \
        "the popover guard must early-return before the stepMonth branches"


def test_urgent_toasts_dwell_longer_than_routine():
    """Re-audit round 4, defect 5: error/urgent toasts carry the longest, most
    action-requiring copy ('see console', 'could not reach the server') but were
    auto-dismissed at a fixed 1800ms — too fast to read. toast() must give urgent
    toasts a longer timeout (a ternary on the urgent flag), matching the ~6s dwell
    of toastAction, so remediation instructions stay actionable."""
    m = re.search(r"function toast\(msg,\s*urgent\)\{(.*?)\}", HTML, re.S)
    assert m, "toast() not found"
    body = m.group(1)
    # the dismissal timer must branch on `urgent` rather than a single constant.
    to = re.search(r"setTimeout\([^,]*,\s*urgent\?(\d+):(\d+)\)", body)
    assert to, "toast() timeout must be a ternary on the urgent flag"
    urgent_ms, normal_ms = int(to.group(1)), int(to.group(2))
    assert urgent_ms > normal_ms, "urgent toasts must dwell longer than routine ones"
    assert urgent_ms >= 4000, "urgent toasts should dwell ~5s so errors are readable"


def test_refresh_button_disabled_during_refresh():
    """Re-audit round 4, defect 4: the Refresh button must be disabled for the
    whole in-flight refresh so a double-click can't POST /api/refresh twice — the
    server is threaded with no lock, so concurrent parse pipelines race on the
    same rules/ and data/ files. b.disabled must be set true at the start and
    reset false on EVERY exit path (catch, !ok branch, and the success path). A
    spinning glyph should also read the busy state visually. The logic lives in a
    shared doRefresh(b) that guards the SPECIFIC button passed in."""
    m = re.search(r"async function doRefresh\(b\)\{(.*?)\n\}", HTML, re.S)
    assert m, "doRefresh() handler not found"
    body = m.group(1)
    assert "b.disabled=true" in body, \
        "refresh must disable the button at the start of the run"
    # re-enabling is centralized in a done() helper that resets disabled+label.
    assert re.search(r"done=\(\)=>\{[^}]*b\.disabled=false", body), \
        "refresh must have a done() helper that re-enables the button"
    # every exit path must re-enable via done(): catch, !ok branch, success path.
    assert body.count("done()") >= 3, \
        "done() must be called on every exit path (catch, !ok, success)"
    # the catch (server-unreachable) path re-enables before its toast.
    assert re.search(r"catch\(e\)\{\s*done\(\)", body), \
        "the server-unreachable catch must re-enable the button"
    # a spinning glyph affordance exists (CSS animation) for the busy state.
    assert 'class="spin"' in body, \
        "the Refreshing… label should carry a spinning glyph"
    assert re.search(r"@keyframes spin\{", HTML) and re.search(r"\.spin\{[^}]*animation:spin", HTML), \
        "a .spin CSS animation must exist for the busy affordance"


def test_add_save_button_guards_against_double_submit():
    """Targeted round 5, ux-blocker defect 1: the Add-transaction Save handler is
    the only create-type (non-idempotent) mutation. Without an in-flight guard a
    rapid double-click fires /api/txn_add twice and silently appends a duplicate
    manual row (the backend derives the id from len(manual)). Mirror the Refresh
    button: consult+set the button's disabled flag at the top, and re-enable on
    EVERY exit path via a finally block so the guard survives the early !error
    return and the load() await."""
    m = re.search(r"\$\('#addSave'\)\.onclick=async\(\)=>\{(.*?)\n\};", HTML, re.S)
    assert m, "#addSave onclick handler not found"
    body = m.group(1)
    # the guard consults the button's own disabled state and bails on re-entrancy.
    assert re.search(r"const b=\$\('#addSave'\)", body), \
        "handler must capture the Save button to latch its busy state"
    assert re.search(r"if\(b\.disabled\)\s*return", body), \
        "handler must bail out when a submit is already in flight"
    assert "b.disabled=true" in body, \
        "handler must disable the Save button at the start of the submit"
    # re-enable must be in a finally so it runs on the early error-return, the
    # success path, AND any thrown exception during the await.
    assert re.search(r"\}finally\{[^}]*b\.disabled=false", body), \
        "handler must re-enable the Save button in a finally block"
    # the disable must precede the await (guard the whole round-trip, not after it).
    disable_at = body.index("b.disabled=true")
    await_at = body.index("await mutate(")
    assert disable_at < await_at, \
        "the button must be disabled BEFORE the txn_add round-trip, not after"


def test_sankey_spend_total_includes_net_negative_categories():
    """Final round, defect 1 (correctness/major): the Cash Flow (Sankey) spend
    total must be computed on the SAME net basis as the Overview's totalSpendAdj —
    i.e. sum ALL non-excluded net catTotals values INCLUDING negatives — not a
    v>0-filtered sum. catTotals() nets each category (charges minus friend
    paybacks/refunds); a category that nets negative in a month (a Venmo/Zelle
    payback or a refund exceeding that month's charges) genuinely reduces real
    spend. If spendTotal dropped those negatives it would (a) report different
    spending than the Overview for the identical month and (b) inflate
    outTotal=saved+spendTotal so the balancing Unallocated / From-reserves
    pseudo-flow is understated by exactly the dropped amount — breaking the
    conservation identity the Sankey's own comment promises to uphold.
    The per-category ribbon list (spendCats) may still clamp v>0 (a negative
    can't render a ribbon width); only the TOTAL must not filter positives."""
    # The Sankey was decomposed: the flow-model math (spendCats/spendTotal) now
    # lives in computeSankeyModel(); renderSankey() does geometry+wiring. Scan
    # BOTH so this invariant is checked wherever the math resides, rather than
    # breaking on a pure code move (the brittleness that motivated the split).
    mm = re.search(r"function computeSankeyModel\(tx\)\{(.*?)\n\}", HTML, re.S)
    mr = re.search(r"function renderSankey\(\)\{(.*?)\n\}", HTML, re.S)
    assert mm or mr, "neither computeSankeyModel() nor renderSankey() found"
    body = (mm.group(1) if mm else "") + "\n" + (mr.group(1) if mr else "")
    # the ribbon list is still allowed to clamp to positive categories...
    assert re.search(r"const spendCats=Object\.entries\(cats\)\.filter\(\(\[c,v\]\)=>v>0", body), \
        "spendCats (the ribbon list) should still filter v>0"
    # ...but spendTotal must NOT be a reduce over the v>0-filtered spendCats.
    assert not re.search(r"const spendTotal=spendCats\.reduce", body), \
        ("spendTotal must not be summed from the v>0-filtered spendCats — that "
         "silently drops net-negative categories and diverges from the Overview")
    # spendTotal must sum ALL non-excluded net category values (incl. negatives),
    # keyed off cats/catTotals without a v>0 gate on the total.
    st = re.search(r"const spendTotal=(.*?);", body)
    assert st, "spendTotal assignment not found"
    expr = st.group(1)
    assert "cats" in expr, "spendTotal must be derived from the net catTotals map"
    assert "v>0" not in expr and "v > 0" not in expr, \
        "the spendTotal expression must not filter to positive categories only"
    assert "EXCLUDED" in expr, \
        "spendTotal must still honor the EXCLUDED (scenario-toggle) filter"


def test_reset_budgets_invalidates_invest_cache():
    """Final round, defect 2 (ux-blocker/major): the 'Reset budgets to formula'
    handler (#sugApply) repaints the budget table with the new formula budgets but
    left the Auto-Invest panel directly above it showing STALE tier / budget-total
    numbers. renderInvest() is a cache — `if(!INVEST){INVEST=await ...}` — so it
    reuses the pre-reset server snapshot unless INVEST is invalidated first. The
    single-line edit handler already does `INVEST=null;` before renderInvest(); the
    reset path must mirror that, or the panel and the table disagree on-screen and
    the user is shown an auto-transfer amount computed against budgets that no
    longer exist. A plain tab switch does NOT fix it (switchTab->renderInvest still
    sees the cached INVEST); only load() or an explicit INVEST=null does."""
    m = re.search(r"\$\('#sugApply'\)\.onclick=async\(\)=>\{(.*?renderInvest\(\);)\};",
                  HTML, re.S)
    assert m, "#sugApply reset handler not found"
    body = m.group(1)
    assert "renderInvest()" in body, "reset handler must re-render the invest panel"
    # INVEST must be invalidated, and BEFORE renderInvest() runs (otherwise the
    # cache guard short-circuits and the stale tiers are repainted).
    assert "INVEST=null" in body, \
        "reset handler must invalidate the cached INVEST before re-rendering"
    assert body.index("INVEST=null") < body.index("renderInvest()"), \
        "INVEST=null must precede renderInvest() so the cache is actually refetched"


def test_income_derived_views_use_complete_income_months():
    """Cash-flow and investing history must not turn uncovered activity into $0 income."""
    assert "function sourceCoversMonth(source,month)" in HTML
    assert "function completeIncomeMonths(until=CURMONTH)" in HTML
    for fn in ("sankeyMonths", "renderTrends", "renderInvesting", "renderCatTrend"):
        m = re.search(r"function %s\((.*?)\)\{(.*?)\n\}" % fn, HTML, re.S)
        assert m, f"{fn}() not found"
        assert "completeIncomeMonths" in m.group(2), \
            f"{fn} must use only complete checking-income months"


def test_empty_state_refresh_has_own_busy_guard():
    """Round 2 (this loop), defect 2 (ux-blocker/major): the empty-state
    #emptyRefresh button must get its OWN guarded refresh handler, not an alias of
    the header button's onclick. The old `$('#emptyRefresh').onclick=$('#refreshBtn').onclick`
    disabled/spun the HEADER button, never the one the user clicked, so the
    empty-state button gave no busy feedback and could be clicked repeatedly —
    launching concurrent /api/refresh pipelines that race on data/ files (the
    server is threaded with no lock)."""
    # the refresh logic is factored into a shared function that guards the passed
    # button, and both entry points call it with their OWN button.
    assert re.search(r"async function doRefresh\(b\)\{", HTML), \
        "refresh logic must be a shared doRefresh(b) that guards the clicked button"
    assert re.search(r"\$\('#refreshBtn'\)\.onclick=\(\)=>doRefresh\(\$\('#refreshBtn'\)\)", HTML), \
        "header button must call doRefresh with its own element"
    assert re.search(r"\$\('#emptyRefresh'\)\.onclick=\(\)=>doRefresh\(\$\('#emptyRefresh'\)\)", HTML), \
        "empty-state button must call doRefresh with its OWN element, not alias the header"
    # the old aliasing pattern must be gone.
    assert "$('#emptyRefresh').onclick=$('#refreshBtn').onclick" not in HTML, \
        "empty-state must not alias the header button's onclick (spins the wrong button)"
    # doRefresh must guard against re-entrancy (the whole point of the busy state).
    dr = re.search(r"async function doRefresh\(b\)\{(.*?)\n\}", HTML, re.S)
    assert dr and "b.disabled" in dr.group(1), \
        "doRefresh must consult/set the clicked button's disabled state"


def test_months_unions_effective_dates_unconditionally():
    """Round 3, defect 1 (correctness/major): months() must union the EFFECTIVE
    (agg_date-based) months for ALL views, not only under SMOOTH. A reconciliation
    group pinned to a report_month past the newest raw statement (e.g. flights
    booked months ahead) carries its net only on its synthetic agg_date entry; if
    months() derives its universe from raw member dates alone (the old non-smooth
    branch), that month is never iterated and the group's entire spend vanishes
    from Overview/donut/Trends/Sankey/Budgets — while the server keys budgets/invest
    purely by agg_date, breaking cross-view agreement.

    The fix: EFFECTIVE.forEach(...s.add...) runs unconditionally, NOT guarded by
    `if(SMOOTH)`."""
    # The month universe is now built once in buildIndexes() (months() reads the
    # prebuilt _monthsList) rather than filtering per call. The INVARIANT is the
    # same: the union of raw txn months AND EFFECTIVE (agg_date) months, done
    # unconditionally (never gated by SMOOTH). Check wherever that union lives.
    mi = re.search(r"function buildIndexes\(\)\{(.*?)\n\}", HTML, re.S)
    assert mi, "buildIndexes() not found"
    body = mi.group(1)
    # both sources unioned into the month set...
    assert "DATA.transactions" in body and "mset.add" in body, \
        "buildIndexes must union raw txn months"
    assert re.search(r"for\(const t of EFFECTIVE\)", body) and "mset.add" in body, \
        "buildIndexes must union EFFECTIVE (agg_date) months too"
    # ...unconditionally — the EFFECTIVE union must NOT sit behind an if(SMOOTH).
    assert not re.search(r"if\(SMOOTH\)[^}]*EFFECTIVE", body), \
        "the EFFECTIVE month union must not be gated behind SMOOTH (round-3 bug)"


def test_recat_offers_whole_merchant_scope_via_override():
    """Round 3, defect 2 (ux-blocker/major): the README promises whole-merchant
    recategorization ('persists to rules/overrides.json'), and ep_override /
    /api/override back it — but the UI never called it. recatTxn() must offer a
    merchant-wide choice and POST to /api/override with merchant_key when the user
    picks it, so the documented one-click merchant fix is actually reachable."""
    # the whole-merchant endpoint must now be invoked from the UI.
    assert "api/override" in HTML, \
        "index.html must call /api/override for whole-merchant recategorization"
    # a dedicated helper POSTs {merchant_key, category} to /api/override.
    rm = re.search(r"async function recatMerchant\((.*?)\)\{(.*?)\n\}", HTML, re.S)
    assert rm, "recatMerchant() helper not found"
    assert re.search(r"mutate\('/api/override',\{merchant_key:", rm.group(2)), \
        "recatMerchant must POST {merchant_key,...} to /api/override"
    # recatTxn must route to it (offer the choice), and the confirm dialog must
    # support the extra (third) button used for the scope choice.
    rt = re.search(r"async function recatTxn\((.*?)\)\{(.*?)\n\}", HTML, re.S)
    assert rt and "recatMerchant(" in rt.group(2), \
        "recatTxn must offer the whole-merchant path via recatMerchant()"
    assert re.search(r"extraLabel", HTML), \
        "confirmModal must accept an extraLabel for the scope choice"


def test_grouped_member_category_is_static_not_editable_dropdown():
    """Round 4, defect 2 (ux-blocker/major): in the Transactions table, a
    reconciliation-group MEMBER row (txnRow rendered with sub=true) must NOT render
    an interactive .catsel category dropdown. group_category (from rules/groups.json)
    governs both the member's display and its totals, so changing the member's
    dropdown snapped the pill back to the group category yet still fired a green
    'Recategorized' toast — a false success + silent revert on a primary action.

    The fix renders the category as STATIC text for grouped members (matching the
    Trips tab pattern at `<td class=\"muted\">${x.group_category||x.category}</td>`),
    while ungrouped rows keep their editable dropdown."""
    # extract the txnRow(t,sub) template function body.
    m = re.search(r"function txnRow\(t,sub\)\{(.*?)\n  \}", HTML, re.S)
    assert m, "txnRow(t,sub) not found"
    body = m.group(1)
    # the category <td> must branch on `sub`: members get static text, non-members
    # get the .catsel dropdown. A single unconditional .catsel is the bug.
    assert "sub" in body and "catsel" in body, \
        "txnRow must still render a .catsel for non-grouped rows"
    # There must be a `sub ? <static> : <select .catsel>` ternary so the dropdown
    # is suppressed for grouped members.
    assert re.search(r"sub\s*\r?\n?\s*\?\s*`<td[^`]*>\$\{esc\(cat\)\}</td>`", body) or \
        re.search(r"sub\?`<td[^`]*>\$\{esc\(cat\)\}</td>`", body), \
        ("grouped members (sub=true) must render the category as static text, not "
         "an editable dropdown that silently reverts")
    # the interactive dropdown must be gated to the ELSE (non-sub) branch.
    assert re.search(r":\s*`<td><select class=\"catsel\"", body), \
        "the .catsel dropdown must be the non-grouped (else) branch of the sub ternary"


def test_multiselect_filter_triggers_carry_the_field_name():
    """#txnCatFilter / #txnSrcFilter are hand-written buttons, so enhanceSelect()
    never names them and their accessible name is whatever the visible .ddlabel
    says — "2 selected" identifies neither field, and both controls end up with the
    SAME name. The markup must carry a static field name, and syncMsFilters (the
    one choke point for every state change) must keep field + value together."""
    for ident, name in (("txnCatFilter", "Filter by category"),
                        ("txnSrcFilter", "Filter by account")):
        tag = re.search(r'<button[^>]*id="%s"[^>]*>' % ident, HTML)
        assert tag, f"#{ident} markup not found"
        assert f'aria-label="{name}"' in tag.group(0), \
            f"#{ident} needs a static aria-label naming the field it filters"
    sync = re.search(r"function syncMsFilters\(\)\{(.*?)\n\}", HTML, re.S)
    assert sync, "syncMsFilters() not found"
    body = sync.group(1)
    assert "setAttribute('aria-label','Filter by category: '" in body, \
        "syncMsFilters must keep the category trigger's name in sync with its value"
    assert "setAttribute('aria-label','Filter by account: '" in body, \
        "syncMsFilters must keep the account trigger's name in sync with its value"


def test_trip_row_category_select_is_named():
    """The per-trip recategorize control is a bare native <select> (.catsel is
    deliberately excluded from enhanceSelect), and a <select> has no accessible-name
    fallback — unnamed, a screen reader hears only "Dining, combo box" for several
    mutually indistinguishable controls that each rewrite a category on change.
    Mirrors the Transactions-tab select, which is already named."""
    m = re.search(r'<select class="catsel tcat"[^>]*>', HTML)
    assert m, ".catsel.tcat markup not found"
    assert "aria-label=" in m.group(0), \
        "the per-trip category select must name the transaction it recategorizes"


def test_make_trip_editable_does_not_announce_a_rejected_create():
    """The .tripedit handler toasted 'Trip is now editable' unconditionally, and
    toast() overwrites the ⚠ mutate() just raised (and downgrades the live region
    from assertive back to polite) — so a rejected create was announced as a
    success. materializeTrip returns null on rejection; gate on it."""
    m = re.search(r"\.tripedit'\)\.forEach\(x=>x\.onclick=async\(\)=>\{(.*?)\}\);", HTML, re.S)
    assert m, ".tripedit handler not found"
    body = m.group(1)
    assert "toast('Trip is now editable')" in body, "the success toast should still exist"
    assert re.search(r"if\(nid\)\s*toast\('Trip is now editable'\)", body), \
        "the success toast must be gated on materializeTrip() actually succeeding"


def test_dismissed_toast_stops_intercepting_clicks():
    """.toast animates opacity only, so without pointer-events the invisible pill
    keeps swallowing clicks at bottom-centre and an Undo button left on it stays in
    the tab order forever — activating it restores a long-forgotten row."""
    base = re.search(r"\.toast\{([^}]*)\}", HTML)
    assert base, ".toast rule not found"
    assert "pointer-events:none" in base.group(1), \
        "a hidden (opacity:0) toast must not intercept clicks"
    shown = re.search(r"\.toast\.show\{([^}]*)\}", HTML)
    assert shown and "pointer-events:auto" in shown.group(1), \
        "a visible toast must be clickable"
    assert re.search(r"function dismissToast\(\)\{(.*?)\n\}", HTML, re.S), \
        "dismissing must go through one helper that also retires the action button"
    body = re.search(r"function dismissToast\(\)\{(.*?)\n\}", HTML, re.S).group(1)
    assert ".toast-action" in body and "remove()" in body, \
        "a dismissed toast must drop its action button out of the tab order"


def test_trips_comparison_rows_fit_a_phone_width():
    """The strip's fixed 150px name column plus a nowrap "$X net · $Y gross" value
    needs ~331px of inner width, so on a phone the value rendered outside the card
    and off-screen (and gave the whole dashboard sideways scroll). The narrow-width
    block must relax both. The columns are declared on the .tcmp CONTAINER (rows
    subgrid onto them so every bar track is the same width), so the phone override
    lives there too."""
    blocks = re.findall(r"@media\(max-width:760px\)\{(.*?)\n  \}", HTML, re.S)
    assert blocks, "narrow-width media block not found"
    narrow = "\n".join(blocks)
    assert re.search(r"\.tcmp\{[^}]*grid-template-columns:minmax\(0", narrow), \
        "the trip name column must be allowed to shrink below 760px"
    assert re.search(r"\.tcmpval\{[^}]*white-space:normal", narrow), \
        "the net/gross value must be allowed to wrap below 760px"


def test_trips_comparison_bar_tracks_share_one_column():
    """Bar length must encode spend and NOTHING else. Per-row grids let the `auto`
    value column size to that row's own label, so the 1fr bar track was a different
    width in every row — a trip labelled just "$240" got a longer bar than a pricier
    trip labelled "$330 net · $405 gross". The columns must live on .tcmp with the
    rows subgridded onto them."""
    tcmp = re.search(r"\n  \.tcmp\{([^}]*)\}", HTML).group(1)
    assert "display:grid" in tcmp and "grid-template-columns:" in tcmp, \
        ".tcmp must own the shared column tracks"
    row = re.search(r"\n  \.tcmprow\{([^}]*)\}", HTML).group(1)
    assert "grid-template-columns:subgrid" in row and "grid-column:1/-1" in row, \
        ".tcmprow must subgrid onto .tcmp's tracks, not declare its own"


def test_group_reporting_month_options_derive_value_and_label_together():
    """viewGroup built each <option value> from toISOString() (UTC) while labelling it
    with toLocaleDateString() (local), so in a UTC+ zone the option reading
    'March 2026' carried value '2026-02' and pinned the group to the wrong month."""
    m = re.search(r"const opts=\[.*?\$\('#gviewMonth'\)\.innerHTML=opts\.join\(''\);",
                  HTML, re.S)
    assert m, "the reporting-month option loop was not found"
    loop = m.group(0)
    assert "toISOString" not in loop, \
        "option values must not come from a UTC Date round-trip"
    assert "addMonths(" in loop and "monthName(ym)" in loop, \
        "value and label must both derive from the same YYYY-MM string"
