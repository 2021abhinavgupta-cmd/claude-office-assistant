# Design: Edit History Panel for Sheets Fullscreen Toolbar

**Date:** 2026-07-31
**Status:** Approved, ready for implementation planning

## Problem

`projects.html`'s Sheets view already tracks the *latest* edit per row (who, when,
what changed — see gotcha #69 in `CLAUDE.md`, implemented via `t.last_edited_by` /
`t.last_edited_at` / `t.last_edited_summary` on each task object, surfaced today as a
small 🕓 icon next to each row's number that shows a toast on click via
`showLastEditedInfo(taskId, clientId)`). To see who edited what, a user currently has
to click through rows one at a time. There's no single place to see "everything that's
been edited in this sheet" at a glance.

## Goal

Add a button next to "Exit Fullscreen" that opens a panel listing every edited row in
the current client's sheet, sorted most-recently-edited first, so a user can see the
overall edit picture without hunting row by row.

## Non-goals

- **Not a full multi-edit audit log.** The app only stores the single latest edit per
  row today (no history of prior edits) — this feature rolls up that existing
  latest-per-row data into one list. It does not add new storage, and does not show
  more than one edit event per row. A true "every edit ever" log was explicitly
  considered and declined (would need a new database table + logging on every save —
  bigger change, not what was asked for).
- Clicking a row in the panel to scroll to / highlight it in the sheet — omitted for
  simplicity, can be added later if wanted.
- Any change to how edits are tracked/stored (`saveSheetRow()`'s existing PATCH +
  `last_edited_by/at/summary` fields are untouched).

## Design

### UI placement and interaction

- New button `🕓 Edit History`, added to the toolbar's right-hand button group,
  immediately before the existing Fullscreen/Exit Fullscreen toggle button
  (`frontend/projects.html`, inside `renderClientSheets()`, right before line ~1112's
  `fs-toggle-btn`).
- Clicking it toggles a dropdown panel positioned directly below the button —
  mirrors the existing `☰ Columns` button/panel pattern exactly
  (`toggleColumnsMenu()` / `#cols-menu-${clientId}` at lines 888-894): a
  `position:relative` wrapper around the button, an absolutely-positioned panel
  `div` with `display:none` by default, toggled via a matching
  `toggleEditHistoryMenu(clientId)` function that also closes any other open
  Edit History panels (in case multiple client cards are expanded at once — same
  reasoning `toggleColumnsMenu` already applies via
  `document.querySelectorAll('[id^="cols-menu-"]')`).
- A new `document.addEventListener('click', ...)` block, mirroring the existing one
  at line 895, closes the panel on outside click (checks
  `e.target.closest('[id^="edit-history-menu-"]')` and
  `e.target.closest('[onclick^="toggleEditHistoryMenu"]')`).

### Panel content

- Built from the same `records` array `renderClientSheets()` already constructs for
  the table body (each record's `.t` is the task object with `last_edited_by` etc.
  already on it — no new data fetch).
- Filter to records where `t.last_edited_by` is truthy, sort by `t.last_edited_at`
  descending (string comparison is safe here — see the existing `// last_edited_at is
  stored as an IST wall-clock string ("YYYY-MM-DD HH:MM:SS")` comment at line 824;
  lexical sort on that format sorts chronologically correctly within the same day/
  timezone, matching how the rest of this file already treats the field).
- Each list line renders: row number (or title/caption if non-empty, falling back to
  "Row N" — mirrors how row identification already works elsewhere in this view),
  editor name (`t.last_edited_by`), relative time (`timeAgo(t.last_edited_at)`, the
  existing helper at line 827), and changed-fields summary (`t.last_edited_summary`,
  only appended if non-empty) — i.e. the exact same three pieces of info
  `showLastEditedInfo()` already assembles into a toast string, just rendered as a
  list instead of one-at-a-time.
- If the filtered list is empty: render "No edits tracked yet." as the panel's only
  content.
- Panel is read-only / informational — no click handlers on individual list items
  (see Non-goals: no scroll-to-row in this iteration).

## Components

### `frontend/projects.html`

- Modify: `renderClientSheets()` — add the new button + panel markup in the toolbar
  section (~line 1090-1113), built inline as part of the existing template literal
  the same way the Columns button/panel already is.
- Add: `toggleEditHistoryMenu(clientId)` function, placed next to
  `toggleColumnsMenu()` (~line 888).
- Add: one new `document.addEventListener('click', ...)` block, placed next to the
  existing one (~line 895), OR extend that existing listener's condition to also
  cover the new panel's id/onclick prefixes — implementer's choice, whichever reads
  cleaner in context; both achieve the same behavior.

## Error handling

None needed — this is a pure client-side render of data already loaded into
`allData`/`records`. No new network calls, no new failure modes.

## Testing

Manual only (matches this codebase's established convention — no automated test
framework, see `CLAUDE.md` → Debugging):

- Open a social-media client's Sheets fullscreen view with at least 2 rows that have
  been edited (have `last_edited_by` set) and at least 1 that hasn't.
- Click `🕓 Edit History`: confirm the panel opens below the button, lists only the
  edited rows, newest edit first, each showing editor/time/changed-fields.
- Confirm the unedited row does NOT appear in the list.
- Click elsewhere on the page: confirm the panel closes.
- Open the Columns menu, then click Edit History: confirm only one panel is open at
  a time (mirrors existing Columns-vs-itself exclusivity — cross-menu exclusivity is
  a nice-to-have, not required, since they're visually distinct triggers).
- If no rows have ever been edited in a client's sheet: confirm the panel shows
  "No edits tracked yet." instead of an empty list.
