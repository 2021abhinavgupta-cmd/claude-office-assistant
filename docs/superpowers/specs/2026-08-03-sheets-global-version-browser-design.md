# Design: Global Whole-Sheet Version Browser (Sheets)

**Date:** 2026-08-03
**Status:** Approved, ready for implementation planning

## Problem

The per-row version history shipped earlier today (see
`2026-08-03-sheets-row-version-history-design.md`) only shows one row's history at a
time, reached via that row's 🕓 icon. The user wants the true Google Sheets "Version
history" experience (referenced two real Sheets screenshots): a single global timeline
of every edit made by anyone to any row, and clicking a past version opens a full-page
view of the **entire sheet grid** as it looked at that moment, with the specific
changed cell highlighted.

## Scope, established during brainstorming

- **Restore stays per-row-only.** Re-confirmed explicitly: even though browsing is now
  whole-sheet, clicking Restore still only reverts the one row that version's edit
  actually changed — never any other row. This preserves the safety property from the
  original per-row design (a shared team sheet, multiple people editing different rows
  concurrently — a whole-sheet restore would silently discard others' edits to
  unrelated rows made after that point).
- **New, separate entry point.** A new toolbar button, `🕐 Version History`, next to the
  existing `Edit History` dropdown (unchanged). Deliberately not merged into the
  per-row overlay already shipped, to avoid risking that just-tested feature.
- **Reconstructing "the sheet at time T"** is done by, for each row, taking the latest
  `sheet_edit_log` snapshot with `edited_at <= T`, falling back to the row's current
  live value if it has no snapshot before T (true for any row never edited since this
  logging started today — there's no historical data before the feature existed, this
  is an inherent, disclosed limitation, not a bug to fix).

## Non-goals

- Whole-sheet restore (see above — explicitly re-confirmed as out of scope).
- Reconstructing sheet state from before `sheet_edit_log` existed (technically
  impossible — no data was ever captured).
- Changing the existing per-row overlay or the existing `Edit History` toolbar dropdown
  — both stay exactly as shipped.

## Design

### Backend: `GET /api/sheets/clients/<clientId>/versions`

New route in `backend/routes/ops.py`, alongside the existing per-row `log-version`/
`versions` routes. Returns **every** `sheet_edit_log` row for that `client_id` (not
filtered by `task_id`), newest first: `{"versions": [{id, task_id, editor_name,
edited_at, snapshot}, ...]}`. No new table — same `sheet_edit_log` data, queried by
`client_id` instead of `task_id`.

### Frontend: new overlay, `#sheet-history-overlay`

A second full-page overlay (parallel to `#row-history-overlay`, not a shared/merged
component — keeps the already-shipped, already-tested per-row view untouched). Opened
by the new `🕐 Version History` toolbar button via `openSheetHistory(clientId)`.

**Sidebar:** every edit event for the client, day-grouped, same visual pattern as the
per-row overlay's sidebar (editor name + time per entry) — just unfiltered by row.

**Main pane, on selecting a version:** for every row currently in that client's sheet,
compute its value **as of that version's timestamp**: the latest `sheet_edit_log` entry
for that row's `task_id` with `edited_at <= selected.edited_at`, or the row's current
live value from `allData` if none exists (never edited before this moment). Render the
full sheet grid (same columns as the live Sheets table) with these reconstructed
values. The specific row (and specific changed field within it, reusing the same
field-diff logic already built for the per-row overlay — compare the selected version's
snapshot for its own row against that row's immediately-prior version) gets a highlight,
matching the reference screenshot's cell-level highlight. All other rows render plainly
as context, same as Google Sheets' "N rows not modified."

### Restore

Selecting a version and clicking Restore operates on **only the one row that version's
edit changed** (`version.task_id`) — calls `applySheetFields()` exactly like the
per-row overlay's restore already does, with that version's `snapshot`. Every other row
shown in the grid is display-only in this view; there is no per-row restore control on
rows other than the one the selected version actually touched.

## Error handling

- `GET /api/sheets/clients/<clientId>/versions` failure: same pattern as the per-row
  overlay — toast + empty-state message in the main pane, not a blank screen.
- Reconstructing a row's at-time-T value never fails (it's a pure client-side reduce
  over already-fetched data) — no error path needed there.

## Testing

Manual only, per established convention:

- Make edits to 3 different rows in the same client's sheet (at least one edit each).
- Open `🕐 Version History`: confirm the sidebar lists all edits across all 3 rows,
  correctly interleaved by time, correct editor names.
- Select an older version: confirm the main pane shows the full grid with that specific
  row/field highlighted, and every other row shown with a sensible value (either its
  own historical snapshot if it was edited before that time, or its current value if
  not).
- Click Restore on that version: confirm only that one row reverts; confirm the other
  two rows' current data is completely unaffected.
- Confirm the existing per-row 🕓 overlay and the toolbar `Edit History` dropdown both
  still work exactly as before, untouched by this addition.
