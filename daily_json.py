"""
daily_json.py — build the JSON bodies for the two daily "derived" pushes that are
NOT a raw CSV upload:

    POST /api/daily/reservation   {property, snapshotDate, pickups[], arrivals[]}
    POST /api/daily/departure     {property, snapshotDate, departures[]}

Unlike the CSV reports (yearly-forecast / cancellation), these endpoints want a
SMALL aggregated JSON, so the scraper reads rows off the VHP page (via a CSV
export or a DOM table — see vhp.read_rows_* / the reservation+departure scrape
flow) and this module turns those rows into the agreed shapes:

    pickups    : new bookings ENTERED yesterday, grouped by (raw) agent name ->
                 [{agent, roomNights, reservations}]
    arrivals   : rooms ARRIVING on prev-day / today / tomorrow ->
                 [{date, rooms}]   (3 entries, ISO dates)
    departures : rooms DEPARTING on prev-day / today / tomorrow ->
                 [{date, rooms}]   (3 entries, ISO dates)

Everything here is PURE (no Selenium, no network) so it is unit-tested in
test_daily_json.py. The actual posting is uploader.post_json(); the actual
row-reading is in vhp.py. The exact column names / date format from the live
pages are passed in by the caller (config), so once we've seen a sample export
we pin them in config.REPORTS without touching this logic.
"""
import csv
import datetime
from collections import OrderedDict

# Date formats VHP has been seen to render in exported CSVs / grids. We try each
# in order when normalizing a row's date cell to an ISO 'YYYY-MM-DD' string.
_DATE_FORMATS = (
    "%Y-%m-%d",      # already ISO
    "%d/%m/%y",      # 29/10/26  (VHP's Date filter format, _VHP_DATE_FMT)
    "%d/%m/%Y",      # 29/10/2026
    "%d-%m-%Y",
    "%d-%m-%y",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d %b %Y",      # 29 Oct 2026
    "%d %B %Y",
    "%Y/%m/%d",
)


def prev_today_tomorrow(today=None):
    """The three ISO dates the arrivals/departures feeds report: yesterday, today,
    tomorrow. (Caller passes `today` in tests; defaults to date.today().)"""
    d = today or datetime.date.today()
    return [(d + datetime.timedelta(days=delta)).isoformat() for delta in (-1, 0, 1)]


def to_iso(value):
    """Normalize one date cell to 'YYYY-MM-DD', or None if it can't be parsed.
    Accepts a date/datetime or a string in any of _DATE_FORMATS."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    # Keep only the date part if a time tags along ("29/10/26 14:30").
    s = s.split(" ")[0] if "/" in s or "-" in s else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_number(value, default=0):
    """Parse a cell to a number (int if whole). Tolerates thousands separators,
    stray spaces and a trailing '.00'. Returns `default` on anything unparseable."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        n = value
    else:
        s = str(value).strip().replace(",", "").replace(" ", "")
        if not s:
            return default
        try:
            n = float(s)
        except ValueError:
            return default
    return int(n) if float(n).is_integer() else n


def _read_text(path):
    """Read a CSV file's text, tolerating VHP's encoding. utf-8-sig strips a BOM;
    fall back to cp1252/latin-1 for the odd non-UTF-8 byte in a guest name."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def read_csv_rows(path):
    """Read a downloaded VHP CSV into a list of dict rows (header-keyed).

    VHP's export starts with a blank line and a row of EMPTY cells BEFORE the real
    header (e.g. 'Rsv No','Reservation Name',... is the 3rd line), so we can't use a
    plain DictReader. We find the header as the first row with any non-empty cell,
    map the rows after it, and skip blank separator rows. Duplicate header names
    (VHP repeats 'Flight Number') collapse to the last column — fine, those aren't
    columns we read."""
    import io
    rows = list(csv.reader(io.StringIO(_read_text(path))))
    header_idx = next((i for i, r in enumerate(rows)
                       if any((c or "").strip() for c in r)), None)
    if header_idx is None:
        return []
    header = [(c or "").strip() for c in rows[header_idx]]
    out = []
    for r in rows[header_idx + 1:]:
        if not any((c or "").strip() for c in r):
            continue  # blank separator row
        d = {}
        for j, key in enumerate(header):
            if key:
                d[key] = r[j] if j < len(r) else ""
        out.append(d)
    return out


def aggregate_pickups(rows, agent_key, nights_key, resid_key=None):
    """Group booking rows by RAW agent name -> [{agent, roomNights, reservations}].

    rows       : list of dict rows (already filtered to "entered yesterday").
    agent_key  : column holding the agent name (kept verbatim, only stripped).
    nights_key : column holding the room-nights for that row (summed per agent).
    resid_key  : OPTIONAL column holding the reservation id. If given, `reservations`
                 counts DISTINCT ids (rows are per room-night); if None, each row is
                 one reservation (count = number of rows).

    Sorted by roomNights desc, then agent asc, for a stable payload.
    """
    buckets = OrderedDict()  # agent -> {"roomNights": n, "resids": set(), "rows": n}
    for r in rows:
        agent = str(r.get(agent_key, "")).strip()
        b = buckets.setdefault(agent, {"roomNights": 0, "resids": set(), "rows": 0})
        b["roomNights"] += _to_number(r.get(nights_key), 0)
        b["rows"] += 1
        if resid_key:
            rid = str(r.get(resid_key, "")).strip()
            if rid:
                b["resids"].add(rid)
    out = []
    for agent, b in buckets.items():
        reservations = len(b["resids"]) if resid_key else b["rows"]
        out.append({"agent": agent,
                    "roomNights": b["roomNights"],
                    "reservations": reservations})
    out.sort(key=lambda e: (-e["roomNights"], e["agent"]))
    return out


def count_rooms_by_date(rows, date_key, dates, rooms_key=None):
    """Rooms per date -> [{date, rooms}] for exactly the ISO `dates` given (in order).

    rows      : list of dict rows (the arrivals/departures grid, any window that
                covers `dates`).
    date_key  : column holding the arrival/departure date for the row.
    dates     : the ISO dates to report (e.g. prev_today_tomorrow()); every one is
                present in the output even if zero.
    rooms_key : OPTIONAL column holding a room count for the row. If None, each row
                counts as one room (one row == one room arriving/departing).
    """
    totals = {d: 0 for d in dates}
    for r in rows:
        iso = to_iso(r.get(date_key))
        if iso in totals:
            totals[iso] += _to_number(r.get(rooms_key), 1) if rooms_key else 1
    return [{"date": d, "rooms": totals[d]} for d in dates]


def normalize_agent(name):
    """Collapse whitespace and strip a trailing comma from a raw VHP agent name, so
    'G2 TRAVEL, ' and 'G2 TRAVEL' (and 'BOOKING.COM  ') group as one. Otherwise the
    name is kept verbatim ('raw agent names')."""
    s = " ".join(str(name or "").split())   # collapse runs of whitespace
    return s.rstrip(",").strip()


def dedupe_reservation_rooms(rows, rsvno_key, room_key):
    """VHP lists ONE ROW PER GUEST: a main guest plus AccGuest/RmSharer rows that
    repeat the same (reservation, room) with Nights duplicated and Room Rate 0.
    Collapse to one row per (reservation, room) — keeping the FIRST (main) row — so
    room-nights / reservation / room counts aren't multiplied by the guest count."""
    seen = set()
    out = []
    for r in rows:
        key = (str(r.get(rsvno_key, "")).strip(), str(r.get(room_key, "")).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def aggregate_pickups_reservations(rows, agent_key, rsvno_key, room_key, nights_key):
    """pickups from the reservation grid -> [{agent, roomNights, reservations}].

    Collapses guest rows first, then groups by NORMALIZED agent name:
      roomNights   = sum of Nights over distinct (reservation, room)
      reservations = count of distinct reservation numbers
    Sorted by roomNights desc, then agent asc.
    """
    deduped = dedupe_reservation_rooms(rows, rsvno_key, room_key)
    buckets = OrderedDict()  # agent -> {"roomNights": n, "resids": set()}
    for r in deduped:
        agent = normalize_agent(r.get(agent_key, ""))
        b = buckets.setdefault(agent, {"roomNights": 0, "resids": set()})
        b["roomNights"] += _to_number(r.get(nights_key), 0)
        rid = str(r.get(rsvno_key, "")).strip()
        if rid:
            b["resids"].add(rid)
    out = [{"agent": a, "roomNights": b["roomNights"], "reservations": len(b["resids"])}
           for a, b in buckets.items()]
    out.sort(key=lambda e: (-e["roomNights"], e["agent"]))
    return out


def distinct_rooms_by_date(rows, date_key, room_key, dates):
    """Rooms per date counting DISTINCT room numbers (VHP repeats a room once per
    guest) -> [{date, rooms}] for exactly `dates`, in order, zero-filled."""
    sets = {d: set() for d in dates}
    for r in rows:
        iso = to_iso(r.get(date_key))
        if iso in sets:
            room = str(r.get(room_key, "")).strip()
            if room:
                sets[iso].add(room)
    return [{"date": d, "rooms": len(sets[d])} for d in dates]


def _envelope(property_code, snapshot_date=None, today=None):
    return {
        "property": property_code.upper(),
        "snapshotDate": snapshot_date or (today or datetime.date.today()).isoformat(),
    }


def build_reservation_payload(property_code, pickups, arrivals,
                              snapshot_date=None, today=None):
    """{property, snapshotDate, pickups, arrivals} for POST /api/daily/reservation."""
    body = _envelope(property_code, snapshot_date, today)
    body["pickups"] = list(pickups or [])
    body["arrivals"] = list(arrivals or [])
    return body


def build_departure_payload(property_code, departures, snapshot_date=None, today=None):
    """{property, snapshotDate, departures} for POST /api/daily/departure."""
    body = _envelope(property_code, snapshot_date, today)
    body["departures"] = list(departures or [])
    return body
