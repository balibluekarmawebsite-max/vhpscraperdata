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

Reservation: ONE pull with Arrival Date = yesterday..tomorrow, then split by the
Arrival column into the three dates (arrivals = distinct rooms/date) and grouped
by agent (pickups).

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


def run_reservation(driver, prop, report, download_dir, today=None,
                    dry_run=False, cfg=None, logger=None):
    """Scrape /fr/reservation for the 3-day arrival window, build + POST the
    reservation JSON. Returns (payload, result)."""
    cfg = cfg or config.UPLOAD
    cols = report["columns"]
    dates = dj.prev_today_tomorrow(today)   # [yesterday, today, tomorrow] ISO
    window = set(dates)

    def _pull(status_mode, suffix):
        """One reservation-grid pull in a given 'Reservation Status' view."""
        variant = dict(report)
        variant["name"] = "{}-{}".format(report["name"], suffix)
        variant["dropdowns"] = {"Reservation Status": status_mode}
        variant["require_dropdowns"] = True    # skip rather than export wrong view
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
    # arriving within the 3-day window. distinct_rooms_by_date (room set) and
    # aggregate_pickups_reservations (dedup by reservation+room) both de-duplicate,
    # so a row that appears in both pulls is counted once.
    in_window = [r for r in rows if dj.to_iso(r.get(cols["arrival"])) in window]

    arrivals = dj.distinct_rooms_by_date(in_window, cols["arrival"], cols["room"], dates)
    pickups = dj.aggregate_pickups_reservations(
        in_window, cols["agent"], cols["rsvno"], cols["room"], cols["nights"])
    payload = dj.build_reservation_payload(prop["code"], pickups, arrivals, today=today)

    _log(logger, "    arrivals: {}".format(arrivals))
    _log(logger, "    pickups: {} agent(s)".format(len(pickups)))
    res = _post(report, payload, cfg, dry_run, logger)
    return payload, res


def run_departure(driver, prop, report, download_dir, today=None,
                  dry_run=False, cfg=None, logger=None):
    """Build + POST the departure JSON for yesterday / today / tomorrow.

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

    def _pull(display_mode, to_date, suffix):
        """One telephone-operator pull in a given Display view + To Date. Returns
        the exported rows ([] if the grid is legitimately empty)."""
        variant = dict(report)
        variant["name"] = "{}-{}".format(report["name"], suffix)
        variant["dropdowns"] = {"Display": display_mode}
        vhp.open_report(driver, variant)
        vhp.dismiss_blocking_dialog(driver)
        vhp.configure_report(driver, variant)
        if not vhp.dropdown_shows(driver, "Display", display_mode):
            raise RuntimeError("[display] {!r} not active".format(display_mode))
        if not vhp.set_single_date(driver, label, to_date):
            raise RuntimeError("[to-date] could not set {!r} to {}".format(
                label, to_date.strftime("%d/%m/%y")))
        time.sleep(2.0)   # let the date-change re-render/auto-refresh settle
        vhp.click_search(driver)
        if vhp.wait_for_report_ready(driver) == "empty":
            return []
        return dj.read_csv_rows(vhp.export_and_download(
            driver, prop, variant["name"], download_dir))

    def _rooms(rows, d_iso):
        return dj.distinct_rooms_by_date(rows, cols["departure"], cols["room"],
                                         [d_iso])[0]["rooms"]

    tomorrow = base + datetime.timedelta(days=1)
    _log(logger, "  room-departure {}: Expected Departure To Date={} (today+tomorrow)"
         .format(prop["code"].upper(), tomorrow.strftime("%d/%m/%y")))
    exp_rows = _pull("Expected Departure", tomorrow, "exp")
    _log(logger, "    {} in-house row(s); depart {}={}, {}={}".format(
        len(exp_rows), t_iso, _rooms(exp_rows, t_iso), m_iso, _rooms(exp_rows, m_iso)))

    # Yesterday: already-departed guests -> the 'Departed' view (best-effort).
    y_rooms = 0
    try:
        _log(logger, "  room-departure {}: Departed To Date={} (yesterday)".format(
            prop["code"].upper(), base.strftime("%d/%m/%y")))
        dep_rows = _pull("Departed", base, "departed")
        y_rooms = _rooms(dep_rows, y_iso)
        _log(logger, "    {} departed row(s); depart {}={}".format(
            len(dep_rows), y_iso, y_rooms))
    except Exception as exc:  # noqa: BLE001 - non-fatal; still post today+tomorrow
        _log(logger, "    (departed pull failed — yesterday may read 0): {}".format(
            vhp._describe(exc)))

    departures = [
        {"date": y_iso, "rooms": y_rooms},
        {"date": t_iso, "rooms": _rooms(exp_rows, t_iso)},
        {"date": m_iso, "rooms": _rooms(exp_rows, m_iso)},
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
        cfg=None, logger=None):
    """Run one DAILY_JSON_REPORTS entry by its kind. Returns (payload, result)."""
    fn = _RUNNERS.get(report.get("kind"))
    if fn is None:
        raise ValueError("unknown daily-json report kind: {!r}".format(report.get("kind")))
    return fn(driver, prop, report, download_dir, today=today,
              dry_run=dry_run, cfg=cfg, logger=logger)
