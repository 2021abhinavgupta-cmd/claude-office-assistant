# Google Sheets Multi-Tab Sync — Design

## Problem

The existing Google Sheets two-way sync (see `docs/superpowers/specs/2026-08-11-google-sheets-two-way-sync-design.md`, CLAUDE.md gotcha #87) only reads/writes a spreadsheet's **first tab**. The user's real workflow builds a new monthly content-calendar tab per client (e.g. "August 2026", "September 2026") inside the same spreadsheet. Every tab after the first is currently invisible to sync — both push (Lumina→Sheet) and the pull webhook (Sheet→Lumina) silently ignore it.

## Goal

Sync every month-named tab (plus a dedicated "Unscheduled" tab for tasks with no due date) in a client's linked spreadsheet, in both directions, while leaving the already-linked MMGA client's existing single-tab sync completely unchanged until it's manually reconnected.

## Non-goals

- Multiple spreadsheet *files* per client (rejected during brainstorming — user confirmed tabs within one file).
- Arbitrary/freeform tab names syncing — only `"<Month> <Year>"` (e.g. "August 2026") and the literal `"Unscheduled"` are recognized; any other tab (notes, scratch, etc.) is never read or written.
- Auto-migrating MMGA's existing tab to the new convention (explicitly deferred by the user — reconnect manually when ready).
- Changing the in-app Sheets *data model* — the month-tab UI in Lumina is a view/filter over the same task list that already exists; it does not introduce per-month storage.

## Tab naming & routing

- Recognized tab name pattern: `^[A-Z][a-z]+ \d{4}$` where the month name is a real month (January…December), e.g. `"August 2026"`. Plus the literal `"Unscheduled"`.
- A task's target tab is derived from its **Post Day / `due_date`** field: `_month_tab_name_for(due_date)` → `"August 2026"`-style name, or `"Unscheduled"` if `due_date` is blank.
- Tab content is otherwise identical to today's single-tab layout: header row `SHEET_HEADER_ROW`, columns A:M (`lumina_task_id` + the 12 Sheets fields).

## Data model change

New column on `google_sheet_links`: `multi_tab INTEGER DEFAULT 0` (added via the standard try/except `ALTER TABLE` pattern in `db.py`, non-breaking).

- Existing rows (i.e. MMGA) default to `0` → every sync function takes the **exact current single-tab code path**, byte-for-byte, for these clients. Nothing about MMGA's behavior changes as part of this feature.
- New connects (`create_google_sheet_link`) always insert `multi_tab=1`.
- Every multi-tab-aware function branches at the top: `if link.get("multi_tab"): <new logic> else: <existing unchanged logic>`.

## Backend: `google_sheets_store.py`

### New helpers
- `list_tabs(spreadsheet_id) -> list[{"name": str, "sheet_id": int}]` — `spreadsheets.get?fields=sheets.properties`.
- `_is_synced_tab_name(name) -> bool` — matches the month pattern or `"Unscheduled"`.
- `_month_tab_name_for(due_date: str) -> str` — parses an ISO `YYYY-MM-DD` string, returns `"<Month> <Year>"`; returns `"Unscheduled"` if blank/unparseable.
- `ensure_tab_exists(spreadsheet_id, sheet_name)` — creates the tab (`batchUpdate` → `addSheet`) plus writes `SHEET_HEADER_ROW` if it doesn't already exist in `list_tabs()`. Idempotent — safe to call on every push.
- `read_all_synced_tabs(spreadsheet_id) -> dict[str, list]` — one `values:batchGet` call across every currently-synced tab's `'<name>'!A:M` range, returns `{tab_name: rows}`. Used by push/delete to locate a task without an N-tabs-worth of round trips.

### Existing I/O helpers gain a `sheet_name` parameter
`read_all_rows`, `write_row`, `append_row`, `write_cell`, `delete_row`, and the sheetId lookup (renamed `_sheet_id_for_tab`) all take `sheet_name: str`, addressing `'{sheet_name}'!A:M` (A1 notation, single quotes doubled if the tab name itself ever contains one — it won't, given the enforced naming pattern, but the escaping is cheap defense-in-depth). Legacy (`multi_tab=0`) callers keep passing the literal first-tab's actual name (fetched once via `list_tabs()[0]["name"]`) — functionally identical to today's unqualified `A:M`, since Sheets defaults an unqualified range to the first tab anyway, but making it explicit removes the implicit dependency now that the function takes a tab param at all.

### Push (`_push_task_to_sheet_locked`)
1. If `not link.get("multi_tab")`: existing code path, unchanged.
2. Otherwise: compute `target_tab = _month_tab_name_for(fields.get("due_date"))`.
3. `read_all_synced_tabs()` → search every tab's rows for `task_id` in column A.
4. Found in `target_tab` already → `write_row` in place (unchanged behavior, just tab-scoped).
5. Found in a **different** tab (the task's due date changed month since the last push) → `ensure_tab_exists(target_tab)`, `append_row` there, then `delete_row` from the old tab (append-then-delete order, so a failure mid-move leaves the row duplicated rather than lost — duplication self-heals on the next reconcile pass via the existing `duplicates` dedup logic, data loss would not).
6. Not found anywhere → `ensure_tab_exists(target_tab)`, `append_row`.
7. `_mark_pushed(task_id)` on success, same as today.

### Delete (`_delete_task_from_sheet_locked`)
- Same all-tabs search via `read_all_synced_tabs()` before removing a row — the task's row could now be in any month tab, not just "the" tab. Tombstone write is unconditional and unaffected (still keyed by `task_id` only, no tab dimension).

### Reconcile (`_reconcile_sheet_rows_locked`)
- Signature changes for `multi_tab` links: accepts `tabs: dict[str, list]` instead of a flat `rows: list`. The legacy branch keeps the exact current `rows: list` signature and logic.
- `seen_ids`/`current`/dedup all operate **globally across every tab in the payload** in one pass — a task's physical tab is just a container; the Post Day *cell value* in the row is still what sets `due_date` on the task (same as today's single-tab behavior). Reconcile does not enforce or correct tab↔due_date consistency — that's push's job on the next Lumina-side save. If a user manually drags a row into the wrong month tab by hand, reconcile will happily set the task's due_date to whatever that row's Post Day cell says, regardless of which tab it's sitting in.
- Row addressing for the new-row id write-back (`write_cell`) becomes tab-scoped: `write_cell(spreadsheet_id, f"'{tab_name}'!A{row_number_within_that_tab}", new_id)`.
- The existing mass-delete safety guard ("skip deletes if the incoming snapshot recognizes zero current tasks") now triggers on the union of all tabs combined being effectively empty, not just one tab.
- Malformed-row isolation, duplicate-id detection, and version logging (`_log_version`) all keep their current per-row behavior, just iterating across tabs instead of one flat list; log messages include the tab name for clarity (`"August 2026 row 5"` instead of just `"row 5"`).

## Apps Script snippet (`_apps_script_snippet` in `routes/sheets_sync.py`)

Regenerated only for `multi_tab=1` links (legacy MMGA keeps getting the current single-tab snippet, unchanged):

```javascript
function onSheetChange(e) {
  var ss = SpreadsheetApp.getActive();
  var monthRe = /^(January|February|March|April|May|June|July|August|September|October|November|December) \d{4}$/;
  var tabs = {};
  ss.getSheets().forEach(function(sheet) {
    var name = sheet.getName();
    if (name === "Unscheduled" || monthRe.test(name)) {
      var data = sheet.getDataRange().getValues();
      tabs[name] = data.slice(1); // drop header row
    }
  });
  UrlFetchApp.fetch("<webhook_url>", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ tabs: tabs }),
    muteHttpExceptions: true
  });
}

function installTrigger() {
  ScriptApp.newTrigger("onSheetChange")
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onChange()
    .create();
}
// Run installTrigger() once manually (Run > installTrigger) to activate sync.
```

Still exactly one trigger, still `onChange` (not `onEdit` — `onChange` is required to catch structural row/tab insert-delete, which `onEdit` misses; this was already the reasoning for the original single-tab design and doesn't change here). The only difference from today's snippet is that the handler loops over matching tabs instead of hardcoding `getSheets()[0]`.

## Webhook route (`routes/sheets_sync.py::sheets_pull_webhook`)

- Accepts **either** shape: `{"tabs": {...}}` (new) or `{"rows": [...]}` (legacy, still sent by any not-yet-reconnected client's already-installed script). Dispatches to `reconcile_sheet_rows` accordingly — the store function itself branches on `link.get("multi_tab")`, so the route just passes through whichever key is present.
- `MAX_WEBHOOK_ROWS` (1000) now caps the **sum of rows across all tabs** in one payload, not one tab — same protective intent (bound Notion/SQLite API calls per webhook call), just applied to the aggregate.

## Frontend: `projects.html`

No change to the in-app Sheets tab's *data* — it still renders Lumina's own task list (Notion/SQLite), never reads the Google Sheet directly. Two changes:

1. **Month filter → month tab strip.** Replace the `<select id="sheet-month-${clientId}">` (line ~1661) with a horizontal tab strip: `All | August 2026 | September 2026 | ... | Unscheduled`. Same `loadMonthFilter`/`saveMonthFilter` localStorage plumbing (`sheetsMonthFilter_<clientId>`), same filtering call site — just a new sentinel value (`"__unscheduled__"`) for the Unscheduled bucket, which today has no dedicated filter (blank-due_date rows only ever show under "All Months"). The "Unscheduled" tab only renders if at least one visible row currently has no `due_date`. Visual style matches real Sheet tabs: rounded top corners, active tab highlighted with `--acc`, matches the `"<Month> <Year>"` label format used on the Google side for parity.
2. **Connect-modal copy.** One added line explaining the tab-naming convention ("name each month tab like \"August 2026\"; unscheduled tasks go to a tab named \"Unscheduled\"") next to the existing Apps Script setup instructions.

## Testing plan

- `pyflakes` clean on `google_sheets_store.py`, `routes/sheets_sync.py`, `db.py`.
- `import app` boots clean, confirms the `multi_tab` column migrates against the real `logs/app.db`.
- `node --check` clean on the edited `projects.html` script block.
- Unit tests against a scratch temp SQLite DB with mocked Sheets/Notion calls (no production data touched), covering: `_month_tab_name_for` (real date, blank date), a push that lands in a brand-new tab (auto-create), a push that moves a task from one month tab to another, a reconcile pass across a multi-tab payload with a duplicate id in different tabs, and the legacy (`multi_tab=0`) path producing byte-identical behavior to today's single-tab code for the same inputs.
- Live verification blocked on reconnecting a real client with `multi_tab=1` and installing the new Apps Script snippet — same credential/live-environment dependency noted throughout gotcha #87's prior rounds. MMGA is deliberately left disconnected from this feature (per user decision) so it is not a candidate for this round's live test; live verification happens on the next client connected after this ships, or on an explicit MMGA reconnect if the user chooses to opt it in later.
