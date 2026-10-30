# VHP daily JSON feeds — dashboard integration note

Two new **daily** pushes from the office-PC scraper, one per property (BKDS /
BKDU / BKV). Unlike the CSV uploads (`/api/import`, `/api/daily/pickup`,
`/api/daily/cancellation`), these send **small aggregated JSON** — the scraper
reads the VHP page, aggregates it, and POSTs the result. **The dashboard builds
the two receiving endpoints to this contract.** Nothing else changes.

- `POST /api/daily/reservation` — arrivals (by date) + pickups (by agent)
- `POST /api/daily/departure` — departures (by date)

Cadence: **once per property per day** (so 3 POSTs/day per endpoint). Each body
carries `snapshotDate` = the date the scraper ran.

---

## 1. `POST /api/daily/reservation`

Source: VHP `/fr/reservation`, filtered to Arrival Date = **yesterday →
tomorrow**, then split by arrival date.

```
POST /api/daily/reservation
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "property": "BKV",
  "snapshotDate": "2026-10-30",
  "pickups": [
    { "agent": "BOOKING.COM",             "roomNights": 9, "reservations": 4 },
    { "agent": "G2 TRAVEL",               "roomNights": 6, "reservations": 2 },
    { "agent": "ALARIC",                  "roomNights": 5, "reservations": 1 },
    { "agent": "[INDIVIDUAL RESERVATION]","roomNights": 4, "reservations": 2 }
  ],
  "arrivals": [
    { "date": "2026-10-29", "rooms": 3 },
    { "date": "2026-10-30", "rooms": 6 },
    { "date": "2026-10-31", "rooms": 2 }
  ]
}
```

Fields:

| field                   | meaning |
|-------------------------|---------|
| `property`              | `BKDS` \| `BKDU` \| `BKV` (upper-case). |
| `snapshotDate`          | the run date, `YYYY-MM-DD`. The anchor: arrivals/pickups cover `snapshotDate−1`, `snapshotDate`, `snapshotDate+1`. |
| `arrivals[]`            | **always 3 entries** — yesterday, today, tomorrow, in that order. |
| `arrivals[].date`       | `YYYY-MM-DD`. |
| `arrivals[].rooms`      | count of **distinct room numbers** arriving that date. (VHP lists one row per guest and rooms are shared, so this is de-duplicated by room, not a row count.) |
| `pickups[]`             | the same arrivals grouped by booking source. Sorted by `roomNights` desc. May be `[]`. |
| `pickups[].agent`       | the **raw** VHP "Reservation Name" (booking source/channel), e.g. `BOOKING.COM`, `G2 TRAVEL`, `ALARIC`, `[INDIVIDUAL RESERVATION]`. Only trailing commas/whitespace are trimmed (so `G2 TRAVEL,` and `G2 TRAVEL` merge). Not mapped to codes. |
| `pickups[].roomNights`  | `Nights` summed over that agent's **distinct (reservation, room)** in the window (accompanying-guest rows are collapsed, so this is not multiplied by guest count). |
| `pickups[].reservations`| count of **distinct reservations** (VHP `Rsv No`) for that agent in the window. |

> **One point to confirm:** `pickups` here is computed over the **whole 3-day
> arrival window** (same rows as `arrivals`), grouped by agent — i.e. "who booked
> the rooms arriving yesterday→tomorrow." The original request worded it as
> "bookings *entered yesterday*" (i.e. by creation date). If the dashboard wants
> pickups scoped differently — **today's arrivals only**, or truly **created
> yesterday** (that comes from a different VHP page, `reservation-by-creation-date`)
> — tell me; it's a small change on the scraper side. Everything else here is final.

---

## 2. `POST /api/daily/departure`

Source: VHP `/to/telephone-operator`, Display = **Expected Departure**. That view
is anchored on a single "To Date" and lists guests **in-house as of** that date,
so the scraper pulls once per target day (To Date = day−1) and counts rooms whose
departure is that day.

```
POST /api/daily/departure
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "property": "BKV",
  "snapshotDate": "2026-10-30",
  "departures": [
    { "date": "2026-10-29", "rooms": 2 },
    { "date": "2026-10-30", "rooms": 4 },
    { "date": "2026-10-31", "rooms": 4 }
  ]
}
```

| field                 | meaning |
|-----------------------|---------|
| `property`            | `BKDS` \| `BKDU` \| `BKV`. |
| `snapshotDate`        | run date, `YYYY-MM-DD`. |
| `departures[]`        | **always 3 entries** — yesterday, today, tomorrow. |
| `departures[].date`   | `YYYY-MM-DD`. |
| `departures[].rooms`  | count of **distinct room numbers** departing that date. |

---

## 3. Auth

Bearer token in `Authorization`, **the same `IMPORT_API_TOKEN`** as `/api/import`
by default. If you'd rather these two endpoints use their own secret, say so and
the scraper will send a different token for them (per-endpoint token file/env).
Both endpoints must require the token.

## 4. Idempotency (important)

The scraper POSTs these **every day**, and may retry on a network error or
`429/5xx`. Make both endpoints **idempotent**: **upsert keyed by
`(property, snapshotDate)`** — a re-post of the same day replaces that day's
row(s), never appends. (A given day can therefore be safely re-pulled.)

## 5. Response

Return `2xx`. `{"ok": true}` is nice but not required — the scraper accepts any
real `2xx` JSON (it does **not** require the `/api/import` `{ok:true}` shape).
Avoid returning the Next.js HTML shell on a cold start; the scraper treats a
2xx-HTML body as a transient cold start and retries.

## 6. Verify

The scraper-side self-test hits:

```
GET /api/daily/reservation?property=BKV
```

Please make that return the latest stored reservation payload for the property
(most recent `snapshotDate`), so a push can be confirmed end-to-end. An
equivalent `GET /api/daily/departure?property=BKV` would help too.

---

### Checklist for the dashboard side
- [ ] `POST /api/daily/reservation` — store `pickups[]` + `arrivals[]`, upsert by `(property, snapshotDate)` (§4)
- [ ] `POST /api/daily/departure` — store `departures[]`, upsert by `(property, snapshotDate)` (§4)
- [ ] Both require the Bearer token (§3)
- [ ] Return `2xx` JSON, not the app shell (§5)
- [ ] `GET /api/daily/reservation?property=BKV` returns the latest payload (§6)
- [ ] Confirm the `pickups` scope (3-day window by agent — see the note in §1)
