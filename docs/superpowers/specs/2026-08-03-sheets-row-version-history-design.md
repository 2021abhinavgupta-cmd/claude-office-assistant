# Design: Full Per-Row Version History + Restore (Sheets)

**Date:** 2026-08-03
**Status:** Approved, ready for implementation planning

## Problem

The existing "last edited" tracking (`t.last_edited_by`/`last_edited_at`/`last_edited_summary`,
see `CLAUDE.md` gotcha #69) and the Edit History toolbar dropdown (added earlier today)
only ever show the **single latest** edit per row. There is no way to see the full
history of every edit made to a row, no way to see the actual old→new value of a
changed field, and no way to undo a bad edit. The user wants Google Sheets-style version
history (referenced a real Sheets "Version history" screenshot): a chronological list of
every past edit with who/when, a diff view, and a restore button.

## Scope, established during brainstorming

- **Restore is required** (not just visibility) — confirmed with the user.
- **Per-row scope, not whole-sheet.** This is a shared team sheet — multiple people edit
  different rows concurrently. A whole-sheet "snapshot at time T" restore (true Google
  Sheets behavior) would silently discard other people's edits to unrelated rows made
  after that timestamp — rejected as unsafe for a multi-editor sheet. Each row has its
  own independent history; restoring a row never touches any other row.
- **Full-page view, not a modal.** Confirmed with the user — closer to the Google Sheets
  reference than an overlay dropdown/toast.
- **Not a new URL/page load.** A full-page *overlay within `projects.html`*, reusing the
  same mechanism the existing Fullscreen-Sheets view already uses (`.fullscreen-sheets`
  class, `toggleFullscreenSheets()`), so it stays fast and keeps `allData` in memory
  rather than triggering a real page navigation (auth reload, full refetch, lost sort/
  scroll state, etc.). "Dedicated full-page view" describes what the user sees, not a
  literal new `.html` file.

## Non-goals

- Whole-sheet/multi-row snapshot restore (see above — explicitly rejected).
- Any change to write frequency patterns like the tab-close-checkout incident from
  earlier today. This feature only writes a log entry on a genuine, already-deduplicated
  edit (see "When it logs" below) — nothing analogous to a per-navigation or per-poll
  write. Called out explicitly because it's the freshest lesson from this same session.
- Editing/deleting past log entries. The log is append-only.
- A separate "who changed what across the whole sheet, full history" view — the existing
  toolbar `Edit History` dropdown (latest-per-row rollup, shipped earlier today) stays as
  the quick overview; this feature is reached per-row via the 🕓 icon and is additive.

## Design

### Storage: `sheet_edit_log` table (new, SQLite, `backend/db.py`)

```sql
CREATE TABLE IF NOT EXISTS sheet_edit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    editor_name TEXT NOT NULL,
    edited_at   TEXT NOT NULL,
    snapshot    TEXT NOT NULL
)
```

Lives in our own SQLite **regardless of whether the task itself is Notion- or
SQLite-backed** — Notion has no changelog concept to build on, and this keeps restore
logic uniform across both client modes.

`snapshot` is a JSON object of the row's **raw field values** at save time — the same
12 raw values `saveSheetRow()` already reads directly from the DOM cells (`creation_date,
due_date, title, type, content, idea, scripts, caption, link, myNotes, assigned_to,
status`), **not** the derived/composite values (`new_title`, `submission_note`) it
computes from them. Storing raw fields (not composites) means restoring a version can
run those raw values back through the exact same composite-building logic
`saveSheetRow()` already uses — one code path for both saving and restoring, no
duplicated title/notes-assembly logic. Storing a full snapshot (all 12 fields) rather
than only the changed subset means the UI can compute a diff for display by simply
comparing snapshot[i] to snapshot[i-1] at render time, and restore is a single "re-apply
this whole snapshot" operation — no reconstruction/replay logic needed.

An index on `(task_id)` is worth adding (`CREATE INDEX IF NOT EXISTS idx_sheet_edit_log_task
ON sheet_edit_log(task_id)`) since every read of this table filters by task_id.

### When it logs

`saveSheetRow()` (`frontend/projects.html`) already computes `changedPayload` — the real
field-level diff — before saving, and already early-returns via
`if (Object.keys(changedPayload).length === 0) return;` when nothing actually changed
(this early-return is what the earlier status-comparison bug fix made reliable — before
that fix, a false-positive diff would have caused erroneous version-log entries too, on
every blur; this feature depends on that fix being correct).

After a successful save (same point where `taskObj.last_edited_by` etc. are already
updated, ~line 2120), the frontend fires one additional fire-and-forget call to a new
`POST /api/sheets/tasks/<taskId>/log-version` endpoint with `{client_id, editor_name,
snapshot: {the 12 raw field values just saved}}`. This is a second small write per real
edit — not a new write *pattern* the way tab-close-checkout was; it scales with actual
edits (inherently infrequent), not with navigation or polling.

### Backend: `POST /api/sheets/tasks/<taskId>/log-version`

New route in `backend/routes/ops.py` (co-located with the other Sheets-related routes
like `notion_update_task`/`sqlite_patch_task`). Body: `{client_id, editor_name,
snapshot}`. Inserts one row into `sheet_edit_log` with `edited_at = now_ist()` (full
timestamp, not just date — reuses the existing IST helper pattern from `utils.py`). No
read-modify-write, no dependency on Notion/SQLite task mode — purely appends to our own
table. Returns `{"success": true}`.

### Backend: `GET /api/sheets/tasks/<taskId>/versions`

New route, same file. Returns all `sheet_edit_log` rows for that `task_id`, ordered
`edited_at DESC`, each with `{id, editor_name, edited_at, snapshot}`. Powers the
full-page view's right-hand version list.

### Frontend: full-page overlay

New function `openRowHistory(taskId, clientId)`, replacing today's
`showLastEditedInfo(taskId, clientId)` as the 🕓 icon's click handler (both the row-icon
in the table and the entry click inside the existing Edit History dropdown). Reuses the
`.fullscreen-sheets`-style overlay pattern already in this file (same
`toggleFullscreenSheets()` mechanism, applied to a new overlay container instead of the
sheet table) rather than a real page navigation.

Layout:
- Header: back arrow (closes overlay, returns to the sheet), selected version's
  timestamp + editor, a **Restore this version** button.
- Main pane: the row's 12 fields as selected-version values, with any field whose value
  differs from the *previous* version in the list highlighted (background color, same
  visual language as the orange highlight in the reference screenshot).
- Right sidebar: every version for this row, `edited_at DESC`, grouped by day (reuses
  the same day-bucketing convention client-dashboard.html already uses for its
  Today/Yesterday/Last week file groupings — see gotcha #32 — for consistency), each
  entry showing editor name + time, clickable to select and re-render the main pane.

### Restore

Clicking **Restore this version** takes the selected version's stored `snapshot` (raw
field values) and re-applies them via the **exact same save path** `saveSheetRow()`
already uses — refactor the composite-building logic (the `newTitle`/`notes` assembly
and the PATCH call) out of `saveSheetRow()` into a shared helper, e.g.
`applySheetFieldsToRow(fields, taskId, clientId, taskObj)`, called by both the normal
save flow and restore. This guarantees restore behaves identically to a normal edit —
including firing a **new** `sheet_edit_log` entry for the restore itself (an explicit
design choice: restoring is itself an edit and should show up in the history, exactly
like Google Sheets' own restore behavior, which creates a new current version rather
than deleting history) — and writing through to Notion or SQLite exactly like any other
save, so no separate restore-specific backend path is needed.

## Error handling

- `log-version` failures are fire-and-forget from the frontend (`.catch(() => {})`,
  matching the existing pattern for non-critical background calls like the presence
  ping in the (currently reverted) tab-close-checkout feature) — a missed log entry
  degrades history completeness but never blocks or fails the actual row save.
- `versions` fetch failures show a toast + empty state ("Couldn't load history") in the
  overlay rather than a blank screen.
- Restore failures surface via the existing `toast(e.message, "err")` pattern already
  used by `saveSheetRow()`'s catch block, since restore reuses that same code path.

## Testing

Manual only (no automated test framework in this repo, per established convention):

- Edit a row's Status, then its Caption, then its Assigned To — three separate saves.
  Open its 🕓 history: confirm 3 versions listed, each showing the correct changed
  field highlighted and the correct old→new values.
- Click an older version, click Restore: confirm the row's live cells update to that
  version's values, confirm a **4th** version entry now appears (the restore itself).
- Confirm restoring one row never changes any other row's data or history.
- Confirm the existing toolbar Edit History dropdown (rollup) still works unchanged.
- Confirm opening/closing the full-page history overlay doesn't lose the sheet's
  current scroll position, sort, or search state (since it's an overlay, not a real
  navigation).
