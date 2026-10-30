"""
test_daily_json.py — unit tests for the daily-JSON aggregation + payload builders.
No browser, no network. Run after any change to daily_json.py:

    C:\\Python38\\python.exe test_daily_json.py
"""
import datetime


def main():
    import daily_json as dj

    results = []

    def check(cond, msg):
        results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + msg)

    # --- date helpers --------------------------------------------------------
    ref = datetime.date(2026, 10, 30)
    check(dj.prev_today_tomorrow(ref) == ["2026-10-29", "2026-10-30", "2026-10-31"],
          "prev_today_tomorrow -> yesterday/today/tomorrow ISO")

    check(dj.to_iso("29/10/26") == "2026-10-29", "to_iso DD/MM/YY")
    check(dj.to_iso("29/10/2026") == "2026-10-29", "to_iso DD/MM/YYYY")
    check(dj.to_iso("2026-10-29") == "2026-10-29", "to_iso passthrough ISO")
    check(dj.to_iso("29 Oct 2026") == "2026-10-29", "to_iso '29 Oct 2026'")
    check(dj.to_iso("29/10/26 14:30") == "2026-10-29", "to_iso drops time part")
    check(dj.to_iso("") is None and dj.to_iso("garbage") is None, "to_iso bad -> None")
    check(dj.to_iso(datetime.date(2026, 10, 29)) == "2026-10-29", "to_iso date obj")

    # --- pickups: group by raw agent, sum nights, count reservations ---------
    rows = [
        {"Agent": "Booking.com", "Nights": "3"},
        {"Agent": "Booking.com", "Nights": "2"},
        {"Agent": "Expedia", "Nights": "5"},
        {"Agent": "Direct", "Nights": "1"},
        {"Agent": "Expedia", "Nights": "1"},
    ]
    pk = dj.aggregate_pickups(rows, "Agent", "Nights")
    # Booking.com: 5 nights / 2 res ; Expedia: 6 / 2 ; Direct: 1 / 1
    check(pk[0] == {"agent": "Expedia", "roomNights": 6, "reservations": 2},
          "pickups: top by roomNights (Expedia 6/2)")
    check({"agent": "Booking.com", "roomNights": 5, "reservations": 2} in pk,
          "pickups: Booking.com 5 nights / 2 reservations")
    check({"agent": "Direct", "roomNights": 1, "reservations": 1} in pk,
          "pickups: Direct 1/1")
    check([e["agent"] for e in pk] == ["Expedia", "Booking.com", "Direct"],
          "pickups: sorted by roomNights desc")

    # resid_key -> reservations = distinct ids (rows are per room-night)
    pernight = [
        {"Agent": "Expedia", "Nights": "1", "ResID": "R1"},
        {"Agent": "Expedia", "Nights": "1", "ResID": "R1"},
        {"Agent": "Expedia", "Nights": "1", "ResID": "R2"},
    ]
    pk2 = dj.aggregate_pickups(pernight, "Agent", "Nights", resid_key="ResID")
    check(pk2 == [{"agent": "Expedia", "roomNights": 3, "reservations": 2}],
          "pickups: resid_key -> distinct reservation count (2), nights summed (3)")

    check(dj.aggregate_pickups([], "Agent", "Nights") == [], "pickups: empty -> []")

    # raw names kept verbatim (only stripped), blank agent still bucketed
    raw = [{"Agent": "  Mr. Smith / TA  ", "Nights": "2"}, {"Agent": "", "Nights": "1"}]
    pr = dj.aggregate_pickups(raw, "Agent", "Nights")
    check(any(e["agent"] == "Mr. Smith / TA" for e in pr), "pickups: raw agent name kept")
    check(any(e["agent"] == "" for e in pr), "pickups: blank agent still counted")

    # --- rooms by date: count rows, or sum a rooms column --------------------
    dates = ["2026-10-29", "2026-10-30", "2026-10-31"]
    arr = [
        {"Arrival": "29/10/26"},
        {"Arrival": "29/10/26"},
        {"Arrival": "30/10/26"},
        {"Arrival": "01/11/26"},   # outside the 3-day window -> ignored
    ]
    by = dj.count_rooms_by_date(arr, "Arrival", dates)
    check(by == [{"date": "2026-10-29", "rooms": 2},
                 {"date": "2026-10-30", "rooms": 1},
                 {"date": "2026-10-31", "rooms": 0}],
          "count_rooms_by_date: counts rows per date, all 3 dates present, zero filled")

    arr2 = [{"Date": "2026-10-30", "Rooms": "4"}, {"Date": "2026-10-30", "Rooms": "3"}]
    by2 = dj.count_rooms_by_date(arr2, "Date", dates, rooms_key="Rooms")
    check(by2[1] == {"date": "2026-10-30", "rooms": 7},
          "count_rooms_by_date: sums a rooms column (4+3=7)")

    # --- agent name normalization -------------------------------------------
    check(dj.normalize_agent("BOOKING.COM  ") == "BOOKING.COM", "normalize: trailing spaces")
    check(dj.normalize_agent("G2 TRAVEL, ") == "G2 TRAVEL", "normalize: trailing comma")
    check(dj.normalize_agent("ALARIC, ") == "ALARIC", "normalize: 'ALARIC,' -> 'ALARIC'")
    check(dj.normalize_agent("[INDIVIDUAL RESERVATION], ") == "[INDIVIDUAL RESERVATION]",
          "normalize: individual reservation")
    check(dj.normalize_agent("") == "", "normalize: empty")

    # --- reservation-grid pickups (guest-row duplication like the real CSV) --
    # Rsv 7294 ALARIC room 505 Nights 5 (main + AccGuest rows); 8742 BOOKING.COM
    # room 201 Nights 2 (main + AccGuest); 9116 INDIVIDUAL room 501 Nights 3;
    # 9517 INDIVIDUAL room 504 Nights 1. All arrive 30/10.
    res = [
        {"Rsv No": "7294", "Reservation Name": "ALARIC, ", "Room Number": "505",
         "Arrival": "30/10/26", "Nights": "5"},
        {"Rsv No": "7294", "Reservation Name": "ALARIC, ", "Room Number": "505",
         "Arrival": "30/10/26", "Nights": "5"},   # AccGuest — must NOT double-count
        {"Rsv No": "8742", "Reservation Name": "BOOKING.COM  ", "Room Number": "201",
         "Arrival": "30/10/26", "Nights": "2"},
        {"Rsv No": "8742", "Reservation Name": "BOOKING.COM  ", "Room Number": "201",
         "Arrival": "30/10/26", "Nights": "2"},
        {"Rsv No": "9116", "Reservation Name": "[INDIVIDUAL RESERVATION], ",
         "Room Number": "501", "Arrival": "30/10/26", "Nights": "3"},
        {"Rsv No": "9116", "Reservation Name": "[INDIVIDUAL RESERVATION], ",
         "Room Number": "501", "Arrival": "30/10/26", "Nights": "3"},
        {"Rsv No": "9517", "Reservation Name": "[INDIVIDUAL RESERVATION], ",
         "Room Number": "504", "Arrival": "30/10/26", "Nights": "1"},
    ]
    deduped = dj.dedupe_reservation_rooms(res, "Rsv No", "Room Number")
    check(len(deduped) == 4, "dedupe: 7 guest rows -> 4 reservation-rooms")

    rpk = dj.aggregate_pickups_reservations(res, "Reservation Name", "Rsv No",
                                            "Room Number", "Nights")
    check({"agent": "ALARIC", "roomNights": 5, "reservations": 1} in rpk,
          "res pickups: ALARIC 5 nights / 1 reservation (not 10/2)")
    check({"agent": "BOOKING.COM", "roomNights": 2, "reservations": 1} in rpk,
          "res pickups: BOOKING.COM 2 / 1")
    check({"agent": "[INDIVIDUAL RESERVATION]", "roomNights": 4, "reservations": 2} in rpk,
          "res pickups: INDIVIDUAL 4 nights (3+1) / 2 reservations")
    check(rpk[0]["agent"] == "ALARIC", "res pickups: sorted by roomNights desc")

    # distinct rooms by arrival date: all 4 rooms arrive 30/10 -> 4 (not 7 rows)
    rd = dj.distinct_rooms_by_date(res, "Arrival", "Room Number",
                                   ["2026-10-29", "2026-10-30", "2026-10-31"])
    check(rd == [{"date": "2026-10-29", "rooms": 0},
                 {"date": "2026-10-30", "rooms": 4},
                 {"date": "2026-10-31", "rooms": 0}],
          "distinct_rooms_by_date: 4 distinct rooms on 30/10, zero elsewhere")

    # --- payload envelopes ---------------------------------------------------
    rp = dj.build_reservation_payload("bkv", pk, by, today=ref)
    check(rp["property"] == "BKV", "reservation payload: property upper-cased")
    check(rp["snapshotDate"] == "2026-10-30", "reservation payload: snapshotDate = today ISO")
    check(rp["pickups"] == pk and rp["arrivals"] == by,
          "reservation payload: carries pickups + arrivals")

    dp = dj.build_departure_payload("BKDS", by2, snapshot_date="2026-10-25")
    check(dp["property"] == "BKDS", "departure payload: property")
    check(dp["snapshotDate"] == "2026-10-25", "departure payload: explicit snapshotDate honored")
    check(dp["departures"] == by2, "departure payload: carries departures")

    # --- read_csv_rows: VHP's blank-line + empty-row preamble before the header --
    import os
    import tempfile
    # Mirrors the real export: line 1 blank, line 2 all-empty cells, line 3 header,
    # then guest-duplicated data rows (FAKE names — no real PII).
    csv_text = (
        "\n"
        '"","","","","","",""\n'
        '"Rsv No","Reservation Name","Guest Name","Room Number","Arrival","Departure","Nights"\n'
        '"7294","ALARIC, ","Guest One MRS","505","30/10/26","04/11/26","5"\n'
        '"7294","ALARIC, ","Guest Two MR","505","30/10/26","04/11/26","5"\n'
        '"8742","BOOKING.COM  ","Guest Three MR","201","29/10/26","01/11/26","2"\n'
        '\n'  # blank separator row in the middle
        '"9517","G2 TRAVEL","Guest Four","504","31/10/26","01/11/26","1"\n'
    )
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(csv_text)
    try:
        prows = dj.read_csv_rows(p)
        check(len(prows) == 4, "read_csv_rows: skips preamble + blank rows -> 4 data rows")
        check(prows[0].get("Rsv No") == "7294" and prows[0].get("Reservation Name") == "ALARIC, ",
              "read_csv_rows: header mapped correctly past the empty preamble")
        pk_real = dj.aggregate_pickups_reservations(prows, "Reservation Name", "Rsv No",
                                                    "Room Number", "Nights")
        check({"agent": "ALARIC", "roomNights": 5, "reservations": 1} in pk_real,
              "read_csv_rows+pickups: ALARIC 5/1 (guest rows collapsed)")
        arr_real = dj.distinct_rooms_by_date(prows, "Arrival", "Room Number",
                                             ["2026-10-29", "2026-10-30", "2026-10-31"])
        check(arr_real == [{"date": "2026-10-29", "rooms": 1},
                           {"date": "2026-10-30", "rooms": 1},
                           {"date": "2026-10-31", "rooms": 1}],
              "read_csv_rows+arrivals: 1 distinct room each on 29/30/31")
    finally:
        os.remove(p)

    passed = sum(results)
    print("\n{}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
