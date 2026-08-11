# Google Sheets Two-Way Sync — Design

## Goal

Let a client (any client, opt-in, not just Social Media clients) be linked to an external Google Sheet such that edits made in Lumina's Sheets tab push to the Google Sheet, and edits made directly in the Google Sheet pull back into Lumina — in near-real-time, in both directions.

## Scope

- Opt-in per client, any client type (not gated to Social Media clients like the existing in-app Sheets tab).
- One Google Sheet (spreadsheet file) per client, linked 1:1 to that client's Lumina Sheets tab.
- New row typed into the Google Sheet auto-creates a task in Lumina.
- Row deleted in the Google Sheet deletes the task in Lumina.
- Internal team editors assumed for v1 (no extra input sanitization tier for external/client editors).
- Real-time sync via an Apps Script trigger installed in the Sheet (not polling).

## Data Model

New table `google_sheet_links` (`backend/db.py`, standard `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS google_sheet_links (
    client_id            TEXT PRIMARY KEY,
    spreadsheet_id        TEXT NOT NULL,
    link_token             TEXT NOT NULL UNIQUE,
    service_account_email TEXT NOT NULL,
    linked_at              TEXT,
    linked_by              TEXT
)
```

`link_token` is a random secret (e.g. `secrets.token_urlsafe(24)`), embedded in the Apps Script snippet the employee pastes into the Sheet, and used as the URL path segment of the pull webhook — it authenticates inbound pull requests without requiring Google OAuth on the webhook itself.

## Sheet Layout

Column A (hidden) = `lumina_task_id`. Columns B onward mirror the 12 existing Sheets fields in the same order already used by `buildSheetRecordsForClient()` / `applySheetFields()` in `projects.html`: `creation_date, due_date, title, type, content, idea, scripts, caption, link, myNotes, assigned_to, status`.

A row with a blank `lumina_task_id` is unsynced — either a brand-new row typed directly into the Sheet, or (transiently) a row Lumina just created that hasn't been written back yet.

## Google API Access

One agency-owned Google Cloud service account. Its JSON key is stored as an env var (`GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON`, the raw JSON string) — never committed to the repo, following the same pattern as other secrets in this codebase (`ANTHROPIC_API_KEY`, `NOTION_TOKEN`, etc., see the Environment Variables table in `CLAUDE.md`). Each client's Sheet must be shared (Editor access) with the service account's email — a one-time manual step during linking, since the agency, not the client, owns Sheet creation in this design.

## Sync: Lumina → Google Sheet (push)

Hook into the existing single choke point for Sheets writes: `applySheetFields()` in `frontend/projects.html`, which already runs on every manual edit, paste, restore, and CSV import. After its existing Notion/SQLite PATCH succeeds, if `client_id` has a `google_sheet_links` row, call a new backend endpoint `POST /api/sheets/push/<task_id>` with the 12 current field values. That endpoint looks up the client's `spreadsheet_id`, finds the row by `lumina_task_id` in column A (or appends a new row if the task has no linked row yet — covers Lumina-created tasks that were never in the Sheet), and writes the row via the Sheets API `values.update`/`values.append`. Fire-and-forget from the frontend's perspective (same pattern as `sheet_edit_log` writes in gotcha #74) — a failed push must never block or fail the save that already succeeded locally.

Clients with no `google_sheet_links` row: zero behavior change, this call is skipped entirely.

## Sync: Google Sheet → Lumina (pull)

An Apps Script **installable `onChange` trigger** (not a simple `onEdit` — `onChange` also fires on row insert/delete, which `onEdit` does not reliably cover) is bound to the Sheet during setup. On any change, the script POSTs the **entire current sheet contents** (all rows, all columns including hidden column A) to:

```
POST /api/sheets/webhook/<link_token>
```

The backend resolves `link_token` → `client_id`, then reconciles by walking every row in the payload:

- `lumina_task_id` blank → create a new task (same path `add-tasks.html`'s workflow-task creation / Sheets "Add Row" already uses), write the generated id back into column A of that row via the Sheets API.
- `lumina_task_id` present, but that id is no longer in the payload at all (row was deleted in the Sheet) → delete the corresponding task in Lumina.
- `lumina_task_id` present and row's fields differ from the task's current stored fields → update the task via the same field-mapping `applySheetFields()` uses server-side equivalent (reuse `notion_update_task()` / `sqlite_patch_task()`, same as a normal Sheets edit).
- `lumina_task_id` present and fields match exactly → skip. This no-op case is what prevents an infinite push↔pull loop: a push-triggered write to the Sheet fires the Sheet's own `onChange`, which pulls back into Lumina, finds nothing actually changed, and stops there instead of re-pushing.

Full-snapshot diffing (not incremental per-cell events) was chosen deliberately: this codebase has a repeated history of diff-only-payload bugs (`CLAUDE.md` gotchas #45, #51, #69) where a partial update silently dropped or misattributed a field. A full snapshot diffed server-side against the DB's current state avoids that whole bug class for this new feature.

## Setup Flow (per client, one-time)

1. Employee clicks "Connect Google Sheet" on the client's Lumina page.
2. Backend generates a `link_token`, creates the `google_sheet_links` row (spreadsheet_id filled in once the employee pastes the URL), and returns a ready-to-paste Apps Script snippet with the token embedded and the service account's email to share the Sheet with.
3. Employee creates (or reuses) a Google Sheet with the 12+1 column layout, shares it with the service account email, pastes the Apps Script snippet into the Sheet's Apps Script editor, runs it once to create the `onChange` trigger, and pastes the Sheet's URL back into the Lumina "Connect Google Sheet" dialog to complete the link.

## Conflict Handling

Last-write-wins by timestamp. Reuses the existing `last_edited_by` / `last_edited_at` fields (gotcha #69) rather than introducing new locking — acceptable given edits from two different surfaces landing within the same second is expected to be rare for this internal tool.

## Security

- `link_token` is the sole authenticator on the pull webhook — unguessable (24 bytes, URL-safe), scoped to exactly one client, never displayed anywhere except the one-time setup dialog.
- Service account JSON key lives only in an env var, consistent with every other secret in this app.
- No new admin-allowlist logic needed — "Connect Google Sheet" follows the existing `_is_admin()` = `bool(user_id)` pattern (any logged-in employee), matching gotcha #60's precedent for this app's other admin-tier actions.

## Out of Scope (v1)

- Client-facing (external) editors on the linked Sheet — no extra sanitization tier built for this yet.
- Multiple Sheets per client, or one Sheet shared across multiple clients.
- Historical backfill — linking a client with existing tasks does not auto-populate the Sheet; the Sheet starts from whatever the employee puts in it, and existing Lumina tasks only appear in the Sheet after their next edit (which triggers a push) or via a manual one-time export the employee does themselves.
