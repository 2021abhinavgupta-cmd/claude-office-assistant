# Design: Instant Tab-Close Checkout with Self-Healing Reconciliation

**Date:** 2026-07-31
**Status:** Approved, ready for implementation planning

## Problem

Attendance checkout (`daily_attendance.checkout_time`) currently only happens via an
explicit action: clicking "Logout" (`window.authLogout()` in `frontend/auth.js`), which
calls `POST /api/attendance/checkout`. Simply closing the browser tab, navigating away,
or losing the laptop lid leaves the employee permanently "checked in" for the day in
`/api/employees/summary` and the attendance dashboard.

A prior attempt to fix this via a `beforeunload` checkout call was implemented and then
**reverted** (see `frontend/auth.js` lines 50-51, comment: "Removed beforeunload checkout
logic to prevent aggressive mid-session checkouts"). Root cause: Lumina is a multi-page
app (`dashboard.html`, `standup.html`, `projects.html`, etc. are separate page loads, not
a client-rendered SPA). `beforeunload`/`pagehide` fires identically on a real tab close
and on clicking any internal link to another Lumina page — the browser gives no way to
distinguish the two client-side. The old fix checked employees out every time they
navigated between pages, and because checkin is one-shot (`ON CONFLICT DO NOTHING`), they
were never marked back "in" for the rest of the day.

## Goal

Treat closing the tab as "checked out," as close to instantly as the browser allows,
**without** reintroducing the internal-navigation false-positive that got the old
implementation reverted.

## Non-goals

- Detecting hard crashes, force-quits, or power loss. No client-side JS can run at that
  point — this would require a server-side idle timeout (a "heartbeat" presence model),
  which was explicitly considered and rejected in favor of this event-driven approach.
- Changing checkin behavior (still first-checkin-of-day-wins).
- Changing the explicit logout flow (`authLogout()`), which already calls checkout today
  and continues to do so unchanged.

## Design

### Mechanism

1. **On `pagehide`** (fires on tab close, browser close, refresh, internal navigation,
   back/forward): `frontend/auth.js` sends `navigator.sendBeacon()` to the existing
   `POST /api/attendance/checkout` endpoint, unchanged. This sets `checkout_time`
   immediately — dashboards reflect "out" right away regardless of the underlying cause.
   `sendBeacon` is used (not `fetch(..., {keepalive: true})`) because it's the API most
   reliably guaranteed by browsers to actually complete delivery during page teardown.

2. **New endpoint `POST /api/attendance/ping`** (`backend/routes/attendance.py`), body
   `{user_id}`. Called by `auth.js`:
   - Once on every protected page's load, after `authGuard()`'s verify succeeds.
   - Every 60 seconds on a `setInterval` while any Lumina page remains open (this is the
     multi-tab safety net, described below — not a presence heartbeat used for detection).

   Logic: look up today's `daily_attendance` row for the user. If `checkout_time` is set
   **and** it was set within the last ~90 seconds, clear it (`checkout_time = NULL`).
   `checkin_time` is left untouched. This is a silent self-heal — **no new row is written
   to the `attendance` audit log** for the cancellation, to avoid cluttering the audit
   trail with routine internal navigation. (The original `'out'` row from step 1 remains
   in the audit log as a minor, harmless artifact of the attempted checkout signal.)
   If `checkout_time` is unset or older than ~90s, the ping is a no-op.

3. **Reconciliation window: 90 seconds.** Chosen as the 60s safety-ping interval plus a
   buffer for network latency. This window must be long enough to cover:
   - **Internal navigation**: new page loads and pings within ~1s typically — comfortably
     inside the window.
   - **Multi-tab close**: employee has two Lumina tabs open, closes one. The surviving
     tab has no page-load event to trigger an immediate ping, but its 60s periodic timer
     will fire and self-heal within ≤60s — still inside the 90s window.
   - A **genuine full close** (no tabs left open anywhere): nothing ever pings again, so
     the checkout set in step 1 simply stands.

### Net effect

- Real tab close → checked out, effectively instantly (the checkout write happens at
  `pagehide` time; nothing ever arrives to undo it).
- Click a link to another Lumina page → checkout fires, then self-heals within about a
  second — invisible to the user, no flicker in practice given typical page-load speed.
- Close one of two open tabs → briefly shows "out" for up to ~60s, then self-heals once
  the surviving tab's periodic ping lands. Accepted tradeoff (explicitly approved) rather
  than adding true multi-tab coordination (e.g. BroadcastChannel), which was judged not
  worth the complexity for an 8-person internal tool.
- Hard crash / power loss → stays checked in for the rest of the day, same as today.
  Explicitly out of scope (see Non-goals).

## Components

### `backend/routes/attendance.py`

- New route: `POST /api/attendance/ping`
  - Body: `{user_id}` (same shape/validation as existing checkin/checkout routes —
    reuse the `_attendance_payload()` helper already present in this file).
  - New helper `_attendance_ping(user_id)`: mirrors `_attendance_checkin`/
    `_attendance_checkout` in structure. Reads today's row; if `checkout_time` is set and
    `now_ist() - checkout_time <= 90s`, `UPDATE daily_attendance SET checkout_time = NULL
    WHERE user_id=? AND date=?`. No `attendance` audit-log insert on this path.
  - No auth beyond the existing pattern used by checkin/checkout (`user_id` in body,
    no session check) — consistent with how those two endpoints already work today. Not
    introducing a new security gap beyond what already exists there.

### `frontend/auth.js`

- Add a `pagehide` event listener (registered once, at module load, alongside the
  existing `authGuard()` IIFE) that reads `agency_portal_user` from `localStorage` and,
  if a `user_id` is present, fires `navigator.sendBeacon(checkoutUrl, blob)` with a JSON
  blob `{user_id}` — mirrors the payload already sent by `authLogout()`'s checkout call.
- Inside `authGuard()`, after a successful verify (i.e., after
  `document.documentElement.style.visibility = 'visible'`), fire one immediate ping to
  `/api/attendance/ping`, then start a `setInterval(() => ping(), 60000)`.
- No changes to `authLogout()` — it already explicitly calls checkout, and since
  `login.html` (where it redirects to) never includes `auth.js`, no reconciliation ping
  can accidentally undo an intentional logout checkout.

### `backend/db.py`

- No schema change. `daily_attendance.checkout_time` is already nullable (checkin inserts
  a row without setting it).

## Error handling

- `sendBeacon` failures are silent/unobservable by design (no callback API) — acceptable,
  matches how `sendBeacon` is meant to be used for teardown-time signals.
- `/api/attendance/ping` on the frontend: fire-and-forget, `.catch(() => {})` like the
  existing checkout call in `authLogout()`. A failed ping just means the safety net didn't
  fire that cycle; the next one (60s later, or next page load) will.
- No behavior change to `/api/attendance/checkout` or `/api/attendance/checkin` — this
  design only adds one new endpoint and one new frontend listener/timer.

## Testing

- Manual: open Lumina, confirm checkin recorded. Navigate between 3-4 different pages in
  quick succession, confirm `checkout_time` stays `NULL` throughout (no false "out").
- Manual: close the tab entirely, confirm (via `/api/attendance/today` or the dashboard)
  that `checkout_time` is set and stays set.
- Manual: open two tabs, close one, confirm status briefly may show "out" then self-heals
  within ~60s while the second tab remains open.
- Manual: explicit Logout button still checks out immediately as before (regression
  check).
