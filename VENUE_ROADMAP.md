# Venue management — where this could go next

Drafted 2026-08-25, after the floor-plan editor shipped. Nothing here is
started. Ordered by what I'd actually do first, not by size.

The general shape of the opportunity: the plan and the booking system are now
two halves of one asset, and most of the value left is in **connecting things
that already exist** rather than building new machinery.

---

## Tier 1 — what you sell on

### 1. Let bookers pick their table on the plan
Today the booking form offers a list of table cards. Show them the actual room
and let them choose "the corner one by the window".

Everything needed exists: the plan, `PlanObject`, and
`GET /venue/admin/layout/occupancy`. This is largely a read-only public mode of
what's already built, plus a public variant of the occupancy endpoint.

The biggest jump in perceived quality for the least new code, and the thing a
venue will point at when explaining why they use us.

### 2. Seat the pairings — BUILT (2026-08-26)

Shipped as `venue_seating.py`, `VenueSeating` / `VenueSeat`, and the Tonight's
tables card on the Diary. Each game is assigned to a real table, held tables the
night turns out not to need are reported as spare, and staff can put them back
on sale — which is where the money is.

Two decisions worth remembering, both narrower than this entry originally
assumed:

* **No `table_id` on `Pairing`, and nothing in the Discord post.** Seating is
  venue-side only. A table number in a published post goes stale the moment
  staff move a game and there is nobody standing in the room to correct it.
  Telling players where to sit is a separate feature with its own failure mode;
  if it's ever wanted, it should read from `VenueSeat` at post time rather than
  writing back onto the pairing.
* **Releasing spare tables is a human decision, not a calculation.** Handing
  four tables back is a promise the venue can't quietly take back when a late
  pairing turns up, so the surplus is shown and a person presses the button.

What's still open: seating the pairings for a night the app DOESN'T run (Magic,
Bolt Action) — there are no pairings to read, so it would need staff to enter
the games, which may not be worth it.

### 3. Check-in on the Tonight view
Staff tap a table: arrived → seated → finished. One status field on
`VenueBooking`, and the plan stops being a picture and becomes the thing behind
the bar.

Cheap, and it unlocks Tier 2 for free.

---

## Tier 2 — what stops them cancelling

### 4. Utilisation reporting
Once check-in gives booked-vs-actual, you can tell a venue "Table 3 runs at
80%, Table 8 has been used twice in two months — move it or lose it", and
"Wednesdays are 40% empty before the club night starts".

This is the report that justifies a subscription. It's the natural extension of
`venue.table_review()`, which already does exactly this shape of thinking for
club nights.

### 5. Waitlist with auto-offer
When a slot is full, join a list. A cancellation offers it to the next person
through the Discord webhook that's already wired. Turns cancellations from lost
revenue into filled tables.

### 6. QR at the table
A code on each table opens that table's page: what's booked, extend my time,
flag a problem. Makes the twin visible to customers rather than only to staff.

---

## Tier 3 — commercial layer

### 7. Deposits and no-shows
`VenueBooking.status` already has `no_show`. Add Stripe on peak slots and
events, and a per-account no-show count feeding
`VenueConfig.max_active_bookings_per_user`.

### 8. Recurring bookings
"Every other Tuesday" for a regular group. The fortnightly cadence logic
already exists in `week_logic` for club nights.

### 9. Multi-site
A chain with several venues. The club finder and per-club scoping already
support it; this is mostly roll-up reporting.

---

## Smaller wins

- **Tonight's sheet** — one-page print of the plan plus bookings and club
  nights, for behind the bar. The PNG export is most of it already.
- **Public "how busy tonight"** page, no login, to pull in walk-ins.
- **Capacity-aware signup** — `day_overview` already computes `outgrown`; warn
  the system admin AT SIGNUP TIME when a club night outgrows its held tables,
  rather than only in the diary.
- **Room occupancy limits** for fire capacity, warned on the plan.

---

## Honest read

Items 1 and 2 are what you sell on. Items 3 and 4 are what stops them
cancelling six months in. Everything below that is worth having and none of it
is what closes the first deal.
