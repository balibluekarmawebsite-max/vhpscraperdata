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
import re
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

    VHP exports carry a PREAMBLE before the real header, in two shapes:
      * reservation/departure: a blank line + a row of EMPTY cells, then the header;
      * 'Reservation By Creation Date': title lines (hotel name/address/period), then
        the header on a later line.
    So we find the header as the first WIDE row (>= 4 non-empty cells) — that skips
    both preamble shapes while matching the always-wide VHP column headers — then map
    the rows after it and skip blank separators. Duplicate header names (VHP repeats
    'Flight Number') collapse to the last column; those aren't columns we read."""
    import io
    rows = list(csv.reader(io.StringIO(_read_text(path))))

    def _nonempty(r):
        return sum(1 for c in r if (c or "").strip())

    header_idx = next((i for i, r in enumerate(rows) if _nonempty(r) >= 4), None)
    if header_idx is None:  # fallback: first row with any content
        header_idx = next((i for i, r in enumerate(rows) if _nonempty(r)), None)
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


def aggregate_pickups_by_creation(rows, agent_key, night_key, qty_key,
                                  resid_key=None, status_key=None, exclude_statuses=()):
    """pickups from 'Reservation By Creation Date' (bookings created on the target
    day) -> [{agent, roomNights, reservations}].

      roomNights   = sum of (Night × Room Quantity)
      reservations = count of bookings
    grouped by NORMALIZED agent (Reservation Name). Rows whose Reservation Status
    matches `exclude_statuses` (e.g. Cancelled / No-show) are dropped first. If
    `resid_key` is given, rows are de-duplicated per reservation (in case the page
    lists one row per guest). Sorted by roomNights desc, then agent asc.
    """
    def _norm_status(s):
        # lower-case, turn hyphens/underscores/punctuation into spaces, collapse —
        # so "No-Show", "No Show" and "NO_SHOW" all compare equal.
        return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()

    excl = [_norm_status(s) for s in exclude_statuses]

    def _excluded(r):
        if not (status_key and excl):
            return False
        st = _norm_status(r.get(status_key, ""))
        # SUBSTRING match so "Cancelled by BookEngine" / "No-Show" are caught.
        return any(term in st for term in excl)

    kept, seen = [], set()
    for r in rows:
        if _excluded(r):
            continue
        if resid_key:
            rid = str(r.get(resid_key, "")).strip()
            if not rid:
                continue  # total / malformed row (no reservation number)
            if rid in seen:
                continue  # one row per guest -> count the reservation once
            seen.add(rid)
        kept.append(r)

    buckets = OrderedDict()
    for r in kept:
        agent = normalize_agent(r.get(agent_key, ""))
        b = buckets.setdefault(agent, {"roomNights": 0, "reservations": 0})
        nights = _to_number(r.get(night_key), 0)
        qty = _to_number(r.get(qty_key), 1) or 1
        b["roomNights"] += nights * qty
        b["reservations"] += 1
    out = [{"agent": a, "roomNights": b["roomNights"], "reservations": b["reservations"]}
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


def _envelope(property_code, snapshot_date=None):
    """Envelope for a daily feed. snapshotDate is OMITTED unless explicitly given:
    the dashboard defaults an absent snapshotDate to YESTERDAY (WITA), which is what
    we want. (Sending today's run date was wrong.) Pass snapshot_date only for a
    backfill of a specific day."""
    env = {"property": property_code.upper()}
    if snapshot_date:
        env["snapshotDate"] = snapshot_date
    return env


def build_reservation_payload(property_code, pickups, arrivals,
                              snapshot_date=None, today=None):
    """{property, [snapshotDate], pickups, arrivals} for POST /api/daily/reservation.
    `today` is kept for signature compatibility but no longer stamps snapshotDate."""
    body = _envelope(property_code, snapshot_date)
    body["pickups"] = list(pickups or [])
    body["arrivals"] = list(arrivals or [])
    return body


def build_departure_payload(property_code, departures, snapshot_date=None, today=None):
    """{property, [snapshotDate], departures} for POST /api/daily/departure.
    `today` kept for signature compatibility; snapshotDate is omitted by default."""
    body = _envelope(property_code, snapshot_date)
    body["departures"] = list(departures or [])
    return body


# VHP's Forecast Room Production "Total" rows carry the word spaced out
# ("T o t a l", "T o t a l  Rp"); after removing spaces they are exactly these.
_ACCOUNT_SKIP = {"total", "totalrp", "grandtotal", "grandtotalrp"}


def aggregate_account_production(rows, account_key, nights_key, revenue_key):
    """Forecast Room Production (Display = 'Guest Type') -> per-account totals:
    [{account, roomNights, revenue}].

    Each row is already one account (VHP groups by Guest Type over the date range).
    The Guest Type cell is 'Account Name,[ optional category]', so the account name
    is the text BEFORE the first comma. Total/subtotal rows and empty rows are
    dropped. roomNights = the 'Room' column (room-nights summed over the range),
    revenue = the 'Room Revenue' column.

    `type` is intentionally NOT emitted: VHP's category suffix (e.g. 'T&T') is not
    the dashboard's OTA/TA/Corporate/Wholesaler taxonomy, so we let the dashboard
    classify by account name (per the feed spec)."""
    out = []
    for r in rows:
        raw = str(r.get(account_key, "") or "").strip()
        if not raw:
            continue
        name = " ".join(raw.split(",")[0].split())          # before 1st comma, tidy spaces
        if not name or name.lower().replace(" ", "") in _ACCOUNT_SKIP:
            continue
        nights = _to_number(r.get(nights_key), 0)
        revenue = _to_number(r.get(revenue_key), 0)
        if not nights and not revenue:
            continue                                          # skip blank/placeholder rows
        # roomNights is a whole count; revenue -> whole rupiah (the dashboard stores
        # integers, and rupiah has no meaningful fractional part).
        out.append({"account": name,
                    "roomNights": int(round(nights)),
                    "revenue": int(round(revenue))})
    # Largest producers first, then by name — a stable, readable payload.
    out.sort(key=lambda e: (-(e["revenue"] or 0), e["account"]))
    return out


def build_account_production_payload(property_code, period, accounts, source=None):
    """{property, period, accounts, source} for POST /api/sales/account-production.
    `period` is the report month 'YYYY-MM' (data is YTD through that month)."""
    body = {"property": property_code.upper(), "period": period,
            "accounts": list(accounts or [])}
    if source:
        body["source"] = source
    return body


def aggregate_room_types(rows, room_type_key, nights_key, revenue_key):
    """Forecast Room by Room Type -> [{roomType, roomNights, revenue}] per room type.

    The report repeats its data: a per-DATE table, then a 'Room Type Summary' table
    (per-room-type totals for the whole date range), both with the same columns. We
    keep the LAST value seen per room type, so the SUMMARY (which comes last = the
    period total per room type) wins -- correct for both a one-day and a full-month
    pull. Skips the repeated header, date sub-headers (a date in the Room Type cell),
    blanks, and TOTAL / Grand TOTAL rows. Revenue is rounded to whole rupiah."""
    seen = OrderedDict()   # roomType -> {"roomNights", "revenue"} (last wins)
    for row in rows:
        rt = " ".join(str(row.get(room_type_key, "") or "").split())
        if not rt:
            continue
        low = rt.lower().replace(" ", "")
        if low in ("roomtype", "total", "grandtotal", "totalrp", "grandtotalrp"):
            continue
        if to_iso(rt) is not None:          # a date sub-header (e.g. 11/11/2026)
            continue
        nights = _to_number(row.get(nights_key), None)
        revenue = _to_number(row.get(revenue_key), None)
        if nights is None and revenue is None:
            continue                         # not a data row
        seen[rt] = {"roomNights": int(round(nights or 0)),
                    "revenue": int(round(revenue or 0))}
    out = [{"roomType": rt, "roomNights": v["roomNights"], "revenue": v["revenue"]}
           for rt, v in seen.items()]
    out.sort(key=lambda e: (-(e["revenue"] or 0), e["roomType"]))
    return out


def build_room_types_payload(property_code, period, room_types, source=None):
    """{property, period, roomTypes, source} for POST /api/sales/room-types.
    `period` is the report month 'YYYY-MM' (actuals for that month)."""
    body = {"property": property_code.upper(), "period": period,
            "roomTypes": list(room_types or [])}
    if source:
        body["source"] = source
    return body


def build_guests_payload(property_code, scope, csv_text, source=None, last_year_csv=None):
    """{property, scope, csv, source, [lastYearCsv]} for POST /api/sales/guests.
    The dashboard parses the raw CSV (and can read property/period from its header),
    so we post the CSV AS-IS -- no column mapping. `scope` is 'MTD' or 'YTD'.
    The CSV holds guest names (PII); callers must not print it."""
    body = {"property": property_code.upper(), "scope": scope, "csv": csv_text or ""}
    if source:
        body["source"] = source
    if last_year_csv:
        body["lastYearCsv"] = last_year_csv
    return body
