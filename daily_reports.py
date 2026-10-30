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

    _log(logger, "  reservation-daily {}: arrivals {} (one pull, Arrival {}..{})".format(
        prop["code"].upper(), dates, dates[0], dates[-1]))
    path = vhp.scrape_report(driver, prop, report, download_dir)
    rows = dj.read_csv_rows(path)
    _log(logger, "    exported {} row(s): {}".format(len(rows), path))

    arrivals = dj.distinct_rooms_by_date(rows, cols["arrival"], cols["room"], dates)
    pickups = dj.aggregate_pickups_reservations(
        rows, cols["agent"], cols["rsvno"], cols["room"], cols["nights"])
    payload = dj.build_reservation_payload(prop["code"], pickups, arrivals, today=today)

    _log(logger, "    arrivals: {}".format(arrivals))
    _log(logger, "    pickups: {} agent(s)".format(len(pickups)))
    res = _post(report, payload, cfg, dry_run, logger)
    return payload, res


def run_departure(driver, prop, report, download_dir, today=None,
                  dry_run=False, cfg=None, logger=None):
    """Scrape /to/telephone-operator once per target date (To Date = D-1, filter
    Depature == D), build + POST the departure JSON. Returns (payload, result)."""
    cfg = cfg or config.UPLOAD
    cols = report["columns"]
    base = today or datetime.date.today()
    dates = dj.prev_today_tomorrow(today)   # [yesterday, today, tomorrow] ISO
    label = report.get("to_date_label", "To Date")

    departures = []
    for offset, d_iso in zip((-1, 0, 1), dates):
        depart_day = base + datetime.timedelta(days=offset)
        to_date = depart_day - datetime.timedelta(days=1)   # in-house as of D-1
        _log(logger, "  room-departure {} {}: To Date={} -> rooms departing {}".format(
            prop["code"].upper(), d_iso, to_date.strftime("%d/%m/%y"), d_iso))

        vhp.open_report(driver, report)             # has a SEARCH button
        vhp.dismiss_blocking_dialog(driver)
        vhp.configure_report(driver, report)        # Display = Expected Departure
        if not vhp.set_single_date(driver, label, to_date):
            raise RuntimeError(
                "[to-date] could not set {!r} to {} — skipping room-departure to "
                "avoid a wrong count".format(label, to_date.strftime("%d/%m/%y")))
        vhp.click_search(driver)
        vhp.wait_for_report_data(driver)
        path = vhp.export_and_download(
            driver, prop, "{}-{}".format(report["name"], d_iso), download_dir)
        rows = dj.read_csv_rows(path)
        rooms = dj.distinct_rooms_by_date(rows, cols["departure"], cols["room"], [d_iso])[0]["rooms"]
        _log(logger, "    {} row(s) exported; {} room(s) depart {}".format(
            len(rows), rooms, d_iso))
        departures.append({"date": d_iso, "rooms": rooms})

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
