"""
inspect_report.py — one-off helper to SEE a report's filter/date controls.

It attaches to a property's Chrome window, opens a report, and prints the filter
fields (labels, data-cy, values) + any date control it finds. We use it to work
out how to drive a control we haven't wired yet — e.g. a NEW report (cancellation
report) before adding it to config.

    python inspect_report.py B                      # existing section B (yearly-forecast)
    python inspect_report.py C --code bkdu          # existing section C on BKDU
    python inspect_report.py --url <report-url>     # a NEW report, by its URL
    python inspect_report.py --url <report-url> --export   # also save a sample CSV (no upload)

--export additionally presses SEARCH and saves one CSV locally (NOT uploaded), so
we can see the report's columns. Send me everything it prints (and the CSV).
"""
import argparse
import sys
import time

import config
import vhp
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock

# JS: list every visible filter control (q-field / data-cy / <select>) with its
# label, data-cy, current value, readonly flag and a short outerHTML — so a
# month/year/period picker is identifiable even if it's not a "Date" field.
_DUMP_CONTROLS_JS = r"""
var out = [];
var seen = [];
function add(e, kind){
  var r = e.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return;           // skip hidden
  if (seen.indexOf(e) !== -1) return; seen.push(e);
  var cy = e.getAttribute('data-cy') || '';
  var l = e.querySelector ? e.querySelector('.q-field__label') : null;
  var label = l ? (l.textContent||'').trim() : '';
  var inp = e.querySelector ? e.querySelector('input, select, textarea') : null;
  var val = inp ? (inp.value || '') : '';
  var ro  = inp ? (inp.readOnly ? 'readonly' : 'editable') : '';
  var ph  = inp ? (inp.placeholder || '') : '';
  var html = (e.outerHTML || '').replace(/\s+/g,' ').slice(0, 300);
  out.push(kind + ' | cy=' + JSON.stringify(cy) + ' label=' + JSON.stringify(label)
           + ' val=' + JSON.stringify(val) + ' ' + ro + ' ph=' + JSON.stringify(ph)
           + '\n      html=' + html);
}
document.querySelectorAll('.q-field').forEach(function(e){ add(e,'q-field'); });
document.querySelectorAll('[data-cy]').forEach(function(e){ add(e,'data-cy'); });
document.querySelectorAll('select').forEach(function(e){ add(e,'select'); });
// anything small whose text is just a year or a month name (a year/month picker display)
var re = /^(20\d\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)\b/i;
document.querySelectorAll('body *').forEach(function(e){
  if (e.children.length > 1) return;
  var t = (e.textContent||'').trim();
  if (t && t.length < 16 && re.test(t)) add(e, 'maybe-picker');
});
return out.slice(0, 60).join('\n');
"""


def main(argv):
    parser = argparse.ArgumentParser(description="Inspect a VHP report's filter/date controls.")
    parser.add_argument("section", nargs="?", help="existing section to inspect: B or C")
    parser.add_argument("--name", help="inspect a REPORTS entry by its exact name "
                                       "(e.g. yearly-forecast-pickup) — needed when several "
                                       "reports share a section letter")
    parser.add_argument("--url", help="inspect ANY report by URL (for a report not yet in config)")
    parser.add_argument("--code", default="bkds", help="which property window to use (default bkds)")
    parser.add_argument("--export", action="store_true",
                        help="also press SEARCH and save ONE sample CSV locally (NOT uploaded)")
    parser.add_argument("--tables", action="store_true",
                        help="also dump every visible table/grid (header + sample rows) — "
                             "use for pages scraped as JSON (reservation / room-departure)")
    parser.add_argument("--search", action="store_true",
                        help="press SEARCH (default filters) + wait for data BEFORE the table "
                             "dump — needed for grids that are empty until searched")
    parser.add_argument("--display", help="set a 'Display' dropdown to this view "
                                          "(e.g. 'Departed') before dumping controls")
    parser.add_argument("--segments", action="store_true",
                        help="dump ALL 'Segment Code' option labels (to tune "
                             "segment_untick to the exact VHP names)")
    parser.add_argument("--diagseg", action="store_true",
                        help="DIAGNOSE why the de-select doesn't reach the export: "
                             "record the SEARCH request body + segment states before/"
                             "after SEARCH, then export and report the result")
    args = parser.parse_args(argv)

    if args.url:
        report = {"name": "adhoc-inspect", "url": args.url, "section": None}
    elif args.name:
        report = next((r for r in config.REPORTS
                       if (r.get("name") or "").lower() == args.name.lower()), None)
        if report is None:
            print("no report named {!r}. Known names: {}".format(
                args.name, ", ".join(r.get("name", "?") for r in config.REPORTS)))
            return 2
    elif args.section:
        report = config.report_by_section(args.section.upper())
        if report is None:
            print("no report for section {!r} (use B or C, or pass --url)".format(args.section))
            return 2
    else:
        print("usage: inspect_report.py <B|C>   OR   inspect_report.py --name <report-name>   "
              "OR   inspect_report.py --url <report-url> [--export]")
        return 2

    prop = next((p for p in config.PROPERTIES if p["code"].lower() == args.code.lower()), None)
    if prop is None:
        print("no property {!r} (use bkds/bkdu/bkv)".format(args.code))
        return 2

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    try:
        kill_stale_chromedrivers()
        print("attaching to {} (port {}) and opening: {}".format(
            prop["code"], prop["debug_port"], report["url"]))
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
        vhp.ensure_logged_in(driver, prop, None, report["url"])
        # Most pages have a SEARCH button (wait for it). Only go gentle for a pure
        # --tables discovery dump on a page that might have none.
        gentle = args.tables and not args.search and not args.export
        vhp.open_report(driver, report, require_search=not gentle)
        time.sleep(2.5)  # let the filter panel hydrate

        if args.diagseg:
            import csv as _csv
            print("\n===== SEGMENT x SHOW-REVENUE DIAGNOSTIC =====")
            print("recorder: {}".format(vhp.install_request_recorder(driver)))
            print("panel:    {}".format(vhp._ensure_filter_panel_open(driver)))
            vhp.set_period(driver, report)   # set the Month exactly like the daily push
            labels = report.get("segment_untick") or ["COMPLIMENT", "HOUSE USE"]

            def _read_occupied(path):
                """Read the exported CSV right here on the PC and return the
                'Room Occupied' value per month (so November shows in the console)."""
                try:
                    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                        rows = list(_csv.reader(fh))
                except Exception as e:  # noqa: BLE001
                    return "(read err {})".format(e)
                header = rows[0] if rows else []
                months = [((h or "").split()[-1] if (h or "").split() else "?") for h in header[1:]]
                for r in rows:
                    if r and r[0].strip().lower() == "room occupied":
                        return ", ".join("{}={}".format(m, (v or "").strip())
                                         for m, v in zip(months, r[1:]) if (v or "").strip())
                return "(no 'Room Occupied' row found)"

            def _export(tag):
                for _cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
                    try:
                        driver.execute_cdp_cmd(_cmd, {"behavior": "allow",
                                                      "downloadPath": config.DOWNLOADS_DIR})
                    except Exception:  # noqa: BLE001
                        pass
                before = vhp.snapshot_dirs(config.DOWNLOAD_WATCH)
                since = time.time()
                vhp.export_print_csv(driver)
                raw = vhp.wait_for_download(config.DOWNLOAD_WATCH, before=before, since=since)
                return vhp.rename_download(raw, prop["code"], "diagseg-" + tag, config.DOWNLOADS_DIR)

            def _run_variant(tag, show_revenue):
                print("\n--- {} : Show Revenue {} + segment de-select ---".format(
                    tag, "ON" if show_revenue else "OFF"))
                try:
                    vhp._set_checkbox(driver, "Show Revenue", show_revenue)
                except Exception as e:  # noqa: BLE001
                    print("   (Show Revenue toggle err: {})".format(vhp._describe(e)))
                print("   segment value after Show Revenue toggle: {!r}".format(vhp._segment_value(driver)))
                try:
                    vhp._segment_untick(driver, labels)
                except Exception as e:  # noqa: BLE001
                    print("   (de-select err: {})".format(vhp._describe(e)))
                print("   segment value just before SEARCH:        {!r}".format(vhp._segment_value(driver)))
                vhp.clear_recorded_requests(driver)
                vhp.click_search(driver)
                vhp.wait_for_report_data(driver)
                time.sleep(1.0)
                print("   SEARCH request(s) captured:")
                for line in vhp.dump_recorded_requests(driver):
                    print("      " + line)
                try:
                    dest = _export(tag)
                    print("   SAVED: {}".format(dest))
                    print("   >>> Room Occupied by month: {}".format(_read_occupied(dest)))
                except Exception as e:  # noqa: BLE001
                    print("   (export err: {})".format(vhp._describe(e)))

            _run_variant("A-segonly", False)   # Show Revenue OFF  -> expect NET  (~341)
            _run_variant("B-revenue", True)    # Show Revenue ON   -> NET or GROSS?

            print("\n----- diagseg done. Compare NOVEMBER (first value) in A vs B: -----")
            print("  A (Show Revenue OFF): November should be NET (~341).")
            print("  B (Show Revenue ON):  if November is GROSS (~353), Show Revenue is")
            print("     overriding the Segment filter -> we must pull occupancy and revenue")
            print("     separately. If it's also ~341, we can safely enable Show Revenue.")
            print("  Send me ALL of the above (the two 'Room Occupied by month' lines + requests).")
            return 0

        if args.display:
            # Switch a 'Display' dropdown (telephone-operator) to a given view
            # BEFORE dumping, so we can see that view's own filter controls
            # (e.g. what date field the 'Departed' view shows).
            try:
                vhp._select_dropdown(driver, "Display", args.display)
                time.sleep(1.5)
                print("\n(set Display = {!r})".format(args.display))
            except Exception as exc:  # noqa: BLE001
                print("\n(could not set Display = {!r}: {})".format(
                    args.display, vhp._describe(exc)))

        if args.segments:
            print("\n===== SEGMENT CODE OPTIONS (exact labels) =====")
            for s in vhp.dump_segment_options(driver):
                print("  | {}".format(s))
            print("Send me this list — I'll set segment_untick to match the real "
                  "Compliment / House Use labels.")

        print("\n===== FIELD LABELS =====")
        print(", ".join(vhp._field_labels(driver)) or "(none)")

        print("\n===== DATE FIELD (reservation-style heuristic) =====")
        f = vhp._find_date_field(driver)
        if f is not None:
            print("FOUND. current value: {!r}".format(vhp._date_field_value(driver, f)))
            print("HTML:\n" + vhp._el_html(driver, f, 2500))
        else:
            print("no 'Date' field matched — this report uses a different control (see below).")

        print("\n===== PAGE DIAG (all inputs + date-looking text) =====")
        print(vhp._dump_date_diag(driver))

        print("\n===== ALL FILTER CONTROLS (q-field / data-cy / select / pickers) =====")
        try:
            print(driver.execute_script(_DUMP_CONTROLS_JS))
        except Exception as exc:  # noqa: BLE001
            print("controls dump failed: {}".format(exc))

        if args.tables:
            if args.search:
                print("\n(pressing SEARCH with default filters to populate the grid...)")
                try:
                    vhp.configure_report(driver, report)   # no-op for an ad-hoc URL
                    vhp.click_search(driver)
                    vhp.wait_for_report_data(driver)
                    time.sleep(1.5)
                except Exception as exc:  # noqa: BLE001
                    print("(search before table dump failed: {})".format(vhp._describe(exc)))
            print("\n===== VISIBLE TABLES / GRIDS (header + sample rows) =====")
            print(vhp.dump_tables(driver))
            print("\nSend me this whole dump — I'll map the columns (agent / room-nights "
                  "/ arrival / departure date) to the JSON the dashboard wants.")

        if args.export:
            print("\n===== SAMPLE CSV via the FULL daily pipeline (scrape_report; NOT uploaded) =====")
            try:
                # Re-assert the download dir (navigation can reset it).
                for _cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
                    try:
                        driver.execute_cdp_cmd(_cmd, {"behavior": "allow",
                                                      "downloadPath": config.DOWNLOADS_DIR})
                    except Exception:  # noqa: BLE001
                        pass
                # Same steps the 6am push uses, on the already-open page (no
                # re-navigation): set the Month, apply the Segment de-select, SEARCH,
                # then export -- so "Room Occupied" here is what the dashboard receives.
                vhp.set_period(driver, report)          # set the Month
                vhp.configure_report(driver, report)    # Segment de-select
                vhp.click_search(driver)
                vhp.wait_for_report_data(driver)
                time.sleep(1)
                before = vhp.snapshot_dirs(config.DOWNLOAD_WATCH)
                since = time.time()
                vhp.export_print_csv(driver)
                raw = vhp.wait_for_download(config.DOWNLOAD_WATCH, before=before, since=since)
                dest = vhp.rename_download(raw, prop["code"], "sample-inspect", config.DOWNLOADS_DIR)
                print("SAVED (not uploaded): {}".format(dest))
                print("Open that CSV — Room Occupied for Nov should now be NET (343), not 355.")
            except Exception as exc:  # noqa: BLE001
                print("scrape failed: {}".format(vhp._describe(exc)))
    finally:
        lock.release()
    print("\n----- done (nothing was uploaded; VHP unchanged). Send me everything above. -----")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
