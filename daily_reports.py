"""
daily_reports.py — the two derived daily feeds that POST aggregated JSON instead
of uploading a raw CSV:

    reservation-daily   -> POST /api/daily/reservation  (pickups[] + arrivals[])
    room-departure-daily-> POST /api/daily/departure     (departures[])

Each one drives the VHP page with the SAME proven helpers the CSV reports use
(open_report / set_period / configure_report / SEARCH / Print->CSV / download),
reads the exported CSV, aggregates it with daily_json, and POSTs the JSON with
uploader.post_json. Config (URL, endpoint, column names, date window) lives in
config.DAILY_JSON_REPORTS; the aggregation is in daily_json (unit-tested).

Reservation: ARRIVALS come from /fr/reservation (two pulls, Reservation + In House
Guest, merged) with Arrival Date = yesterday..tomorrow, split by the Arrival column
(arrivals = distinct rooms/date). PICKUPS come from a SEPARATE page, 'Reservation
By Creation Date', filtered to bookings CREATED yesterday, grouped by Reservation
Name, RN = Night × Room Quantity, excluding Cancelled / No-show (a booking created
yesterday can arrive any future date, so the arrival view can't produce pickups).

Departure: the Expected-Departure view is anchored on a single "To Date" that
lists guests in-house AS OF that date (Arrival <= ToDate, Departure > ToDate), so
rooms departing on date D come from the pull with To Date = D-1, filtered to
Depature == D. That's one pull per target date (3 per run).

dry_run=True scrapes + builds + returns the payload WITHOUT posting, so the
numbers can be eyeballed before trusting the feed (run_daily_json.py --dry-run).
"""
import datetime
import time

import config
import daily_json as dj
import uploader
import vhp


def _log(logger, msg):
    (logger.info if logger else print)(msg)


def _post(report, payload, cfg, dry_run, logger=None):
    """POST the JSON body (or, in dry-run, just show it). Returns the result dict."""
    up = report.get("upload") or {}
    if dry_run or not up.get("url"):
        _log(logger, "    (dry-run) NOT posting. Payload:\n{}".format(_pretty(payload)))
        return {"_dry_run": True, "_payload": payload}
    token = uploader._resolve_token(up, cfg)
    res = uploader.post_json(payload, up["url"], token,
                             bool(up.get("require_ok", False)), cfg)
    _log(logger, "    POST {} -> {} ({})".format(
        up["url"], res.get("_status"), res.get("_timings")))
    return res


def _pretty(payload):
    import json
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _reservation_pickups(driver, prop, report, download_dir, base, logger):
    """PICKUPS for the reservation feed, from a DIFFERENT page than arrivals:
    'Reservation By Creation Date' filtered to bookings CREATED yesterday,
    grouped by Reservation Name, RN = Night × Room Quantity, excluding Cancelled /
    No-show (dashboard §13). A booking created yesterday can arrive on any future
    date, so the arrival view can't produce this — it needs its own pull.

    Best-effort: a failed pull logs and returns [] (empty pickups) rather than
    aborting the whole reservation feed, so arrivals still post.
    """
    src = report.get("pickups_source")
    if not src:
        return []
    y_iso = (base - datetime.timedelta(days=1)).isoformat()   # created yesterday
    variant = dict(src)
    variant["name"] = "{}-pickups".format(report["name"])
    variant["period"] = {"start": y_iso, "end": y_iso}
    variant["period_required"] = True   # skip rather than export an unfiltered page
    try:
        rows = dj.read_csv_rows(vhp.scrape_report(driver, prop, variant, download_dir))
    except Exception as exc:  # noqa: BLE001 - non-fatal; post arrivals with empty pickups
        _log(logger, "    (pickups pull failed — sending empty pickups): {}".format(
            vhp._describe(exc)))
        return []
    c = src["columns"]
    pickups = dj.aggregate_pickups_by_creation(
        rows, c["agent"], c["night"], c["qty"],
        resid_key=c.get("resid"), status_key=c.get("status"),
        exclude_statuses=src.get("exclude_statuses", ()))
    _log(logger, "    pickups source (created {}): {} row(s) -> {} agent(s)".format(
        y_iso, len(rows), len(pickups)))
    return pickups


def run_reservation(driver, prop, report, download_dir, today=None,
                    dry_run=False, cfg=None, logger=None, snapshot_date=None):
    """Scrape /fr/reservation for the 3-day arrival window, build + POST the
    reservation JSON. Returns (payload, result).

    snapshot_date (ISO 'YYYY-MM-DD'): stamp the payload's snapshotDate — used by the
    section-D re-pull to target a specific day. The scheduled daily run leaves it
    None, so the dashboard defaults snapshotDate to yesterday (WITA) as before."""
    cfg = cfg or config.UPLOAD
    cols = report["columns"]
    base = today or datetime.date.today()
    dates = dj.prev_today_tomorrow(today)   # [yesterday, today, tomorrow] ISO
    window = set(dates)
    # Arrival RANGE = yesterday..tomorrow, as EXPLICIT dates anchored to `base` (not
    # a from_days/to_days offset, which _period_range resolves against the system
    # clock — that would disagree with `dates` when a `today` override is used).
    arr_range = {"start": dates[0], "end": dates[-1]}

    def _pull(status_mode, suffix):
        """One reservation-grid pull in a given 'Reservation Status' view."""
        variant = dict(report)
        variant["name"] = "{}-{}".format(report["name"], suffix)
        variant["dropdowns"] = {"Reservation Status": status_mode}
        variant["require_dropdowns"] = True    # skip rather than export wrong view
        variant["period"] = arr_range
        rows = dj.read_csv_rows(vhp.scrape_report(driver, prop, variant, download_dir))
        _log(logger, "    [{}] {} row(s)".format(status_mode, len(rows)))
        return rows

    _log(logger, "  reservation-daily {}: arrivals {} (Arrival {}..{})".format(
        prop["code"].upper(), dates, dates[0], dates[-1]))

    # No single VHP view spans all three days: 'Reservation' shows NOT-yet-arrived
    # (today + tomorrow), and once a guest arrives they drop into 'In House Guest'
    # (so yesterday reads 0 under 'Reservation'). Pull both and merge.
    rows = _pull("Reservation", "res")                         # today + tomorrow
    try:
        rows = rows + _pull("In House Guest", "inhouse")       # yesterday (arrived)
    except Exception as exc:  # noqa: BLE001 - non-fatal; still post the rest
        _log(logger, "    (in-house pull failed — yesterday may read 0): {}".format(
            vhp._describe(exc)))

    # The In-House pull also returns guests who arrived earlier, so keep only rows
    # arriving within the 3-day window. distinct_rooms_by_date works on a room SET
    # per date, so a room that appears in both pulls is counted once.
    in_window = [r for r in rows if dj.to_iso(r.get(cols["arrival"])) in window]

    arrivals = dj.distinct_rooms_by_date(in_window, cols["arrival"], cols["room"], dates)

    # PICKUPS come from their own page ('Reservation By Creation Date', bookings
    # created yesterday) — NOT from these arrival rows, which group by arrival date.
    pickups = _reservation_pickups(driver, prop, report, download_dir, base, logger)
    payload = dj.build_reservation_payload(prop["code"], pickups, arrivals,
                                           snapshot_date=snapshot_date)

    _log(logger, "    arrivals: {}".format(arrivals))
    _log(logger, "    pickups: {} agent(s)".format(len(pickups)))
    res = _post(report, payload, cfg, dry_run, logger)
    return payload, res


def run_departure(driver, prop, report, download_dir, today=None,
                  dry_run=False, cfg=None, logger=None, snapshot_date=None):
    """Build + POST the departure JSON for yesterday / today / tomorrow.

    snapshot_date is accepted for a uniform dispatch signature but intentionally
    NOT used: the /api/daily/departure contract reads dates from departures[] and
    wants no snapshotDate.

    The telephone-operator 'To Date' picker only allows TODAY or later (past dates
    are disabled), so 'Expected Departure, To Date = T' means in-house guests
    expected to depart ON OR BEFORE T. Thus:
      * today + tomorrow  -> 'Expected Departure', To Date = tomorrow, split by the
        Departure column.
      * yesterday          -> those guests have already checked out, so they are in
        the 'Departed' view (best-effort; if it can't be read, yesterday reads 0).
    """
    cfg = cfg or config.UPLOAD
    cols = report["columns"]
    base = today or datetime.date.today()
    y_iso, t_iso, m_iso = dj.prev_today_tomorrow(today)   # yesterday/today/tomorrow
    label = report.get("to_date_label", "To Date")

    def _pull(display_mode, suffix, to_date=None, period=None):
        """One telephone-operator pull in a given Display view. The date filter
        differs by view: 'Expected Departure' has a single 'To Date' (pass
        to_date); 'Departed' has a 'From - Until' RANGE (pass period, set via
        set_period). Returns the exported rows ([] if the grid is empty). Each step
        names itself in any error, so a failure reads like '[search] ...'."""
        def step(name, fn):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("[{}] {}".format(name, vhp._describe(exc)))

        variant = dict(report)
        variant["name"] = "{}-{}".format(report["name"], suffix)
        variant["dropdowns"] = {"Display": display_mode}
        if period is not None:
            variant["period"] = period
        step("open", lambda: vhp.open_report(driver, variant))
        vhp.dismiss_blocking_dialog(driver)
        step("configure", lambda: vhp.configure_report(driver, variant))
        if not vhp.dropdown_shows(driver, "Display", display_mode):
            raise RuntimeError("[display] {!r} not active".format(display_mode))
        if to_date is not None:
            ok = step("set to-date", lambda: vhp.set_single_date(driver, label, to_date))
            what = to_date.strftime("%d/%m/%y")
        else:
            ok = step("set range", lambda: vhp.set_period(driver, variant))
            what = str(period)
        if not ok:
            raise RuntimeError("[date] could not set {}".format(what))
        time.sleep(2.0)   # let the date-change re-render/auto-refresh settle
        step("search", lambda: vhp.click_search(driver))
        if step("wait ready", lambda: vhp.wait_for_report_ready(driver)) == "empty":
            return []
        return step("export", lambda: dj.read_csv_rows(vhp.export_and_download(
            driver, prop, variant["name"], download_dir)))

    def _rooms(rows, d_iso):
        return dj.distinct_rooms_by_date(rows, cols["departure"], cols["room"],
                                         [d_iso])[0]["rooms"]

    tomorrow = base + datetime.timedelta(days=1)
    _log(logger, "  room-departure {}: Expected Departure To Date={} (today+tomorrow)"
         .format(prop["code"].upper(), tomorrow.strftime("%d/%m/%y")))
    exp_rows = _pull("Expected Departure", "exp", to_date=tomorrow)
    _log(logger, "    {} in-house row(s); depart {}={}, {}={}".format(
        len(exp_rows), t_iso, _rooms(exp_rows, t_iso), m_iso, _rooms(exp_rows, m_iso)))

    # Already-departed guests (checked out) -> the 'Departed' view, whose date
    # filter is a FROM-UNTIL range (not a single To Date). Pull yesterday..today so
    # we also catch guests who already checked out TODAY (they've left the
    # Expected-Departure view). Best-effort: if it can't be read, we still post
    # whatever Expected Departure gave. Today's total = either view (deduped by room).
    dep_rows = []
    try:
        _log(logger, "  room-departure {}: Departed range {}..{} (already checked out)"
             .format(prop["code"].upper(), y_iso, t_iso))
        # Explicit dates anchored to the same effective day (yesterday..today), NOT
        # a from_days/to_days offset — _period_range resolves offsets against the
        # system clock, which would disagree with y_iso/t_iso under a `today` override.
        dep_rows = _pull("Departed", "departed",
                         period={"start": y_iso, "end": t_iso})
        _log(logger, "    {} departed row(s); depart {}={}".format(
            len(dep_rows), y_iso, _rooms(dep_rows, y_iso)))
    except Exception as exc:  # noqa: BLE001 - non-fatal; still post today+tomorrow
        _log(logger, "    (departed pull failed — yesterday may read 0): {}".format(
            vhp._describe(exc)))

    departures = [
        {"date": y_iso, "rooms": _rooms(dep_rows, y_iso)},               # departed
        {"date": t_iso, "rooms": _rooms(exp_rows + dep_rows, t_iso)},    # either view
        {"date": m_iso, "rooms": _rooms(exp_rows, m_iso)},               # in-house
    ]
    payload = dj.build_departure_payload(prop["code"], departures, today=today)
    _log(logger, "    departures: {}".format(departures))
    res = _post(report, payload, cfg, dry_run, logger)
    return payload, res


# Dispatch by report["kind"], so a runner (or main.py) can handle both uniformly.
_RUNNERS = {
    "json_reservation": run_reservation,
    "json_departure": run_departure,
}


def run(driver, prop, report, download_dir, today=None, dry_run=False,
        cfg=None, logger=None, snapshot_date=None):
    """Run one DAILY_JSON_REPORTS entry by its kind. Returns (payload, result).
    snapshot_date is forwarded to the runner (used by the section-D re-pull)."""
    fn = _RUNNERS.get(report.get("kind"))
    if fn is None:
        raise ValueError("unknown daily-json report kind: {!r}".format(report.get("kind")))
    return fn(driver, prop, report, download_dir, today=today,
              dry_run=dry_run, cfg=cfg, logger=logger, snapshot_date=snapshot_date)
