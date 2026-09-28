"""Automatic pruning of old, low-value rows -- the durable, free alternative
to paying for more Railway volume every time the database grows.

Context (2026-09-28 disk-full incident): nothing in this codebase ever
deletes old rows from its append-only log/history tables. usage_logs grows
one row per AI call forever, wa_action_log one row per WhatsApp bot write
forever, sheet_edit_log one row per Sheets cell edit forever. None of that
is a bug on its own -- it's just that nothing ever cleans it up, so the
database can only ever grow, and a small Railway volume will eventually
fill up again from pure normal usage even with every other fix in place.

Scope, deliberately conservative -- only tables where a verified-safe
retention window exists:

  usage_logs      -- individual per-call detail beyond the window is lost
                     (recent-calls list, CSV export), but EVERY aggregate
                     number the dashboard shows (monthly spend, all-time
                     spend) is verified to live in a SEPARATE, already-
                     persisted `budget` table row, untouched by this -- see
                     budget_tracker.py's get_usage_summary(). The one
                     exception is the WhatsApp all-time cost/calls/tokens
                     stat, which IS computed by scanning every usage_logs
                     row live -- so before deleting old WhatsApp rows,
                     their totals are folded into a persisted app_settings
                     counter first (see _fold_whatsapp_totals below), and
                     budget_tracker.py reads that counter on top of the
                     live scan so the "all-time" number stays honest.
  wa_action_log   -- pure audit trail (GET /api/companion/wa-audit), no
                     other code aggregates or restores from it.
  wa_call_outbox  -- only ever-terminal rows (sent/failed/expired) pruned;
                     a 'pending' row is never touched regardless of age.

  sheet_edit_log  -- backs the Version History / Restore feature (gotchas
                     #74/#75), so a blanket age-based delete was originally
                     left out entirely (a human decision, not automatic).
                     2026-09-28 round 2: found this table at 326MB of a
                     350MB database -- one client's Google Sheets sync
                     reconciliation burst logged ~5,900 versions for a
                     single task in two days (a runaway-sync failure mode,
                     not normal editing). Rather than an age cutoff (which
                     would be the same unreviewed feature-loss decision as
                     before), this caps each TASK to its most recent
                     SHEET_EDIT_LOG_KEEP_PER_TASK versions -- generous
                     enough that no realistic manual editing pattern is
                     ever affected (a task with fewer versions than the cap
                     is completely untouched), while closing off unbounded
                     growth from any future sync malfunction. Restore still
                     works for every task; only versions beyond what anyone
                     would plausibly restore to are pruned.

Deliberately NOT touched here, even though they can grow large:
  standup_tasks   -- actively queried by date across arbitrary past ranges
                     (Team Standups history picker, Velocity chart) --
                     needs its own careful pass, not a blanket delete.
  conversations   -- real chat history. Never auto-deleted, period.
  followup_log    -- already has its own prune_log(days=30) in
                     followups.py, run at the end of every sweep; not
                     duplicated here.

Each rule is independent and wrapped in its own try/except -- one table's
failure never blocks the others. Runs on Railway's APScheduler (see
task_scheduler.py), not the laptop -- pure DB/filesystem work, nothing
WhatsApp-related, so no bridge/session-fork risk.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

# Default retention windows (days). Conservative on purpose -- these are
# individual-record-detail cutoffs, not "delete the business fact."
USAGE_LOGS_RETENTION_DAYS = 180
WA_ACTION_LOG_RETENTION_DAYS = 180
WA_CALL_OUTBOX_RETENTION_DAYS = 30
SHEET_EDIT_LOG_KEEP_PER_TASK = 30

_WA_PRUNED_COST_KEY = "wa_alltime_pruned_cost"
_WA_PRUNED_CALLS_KEY = "wa_alltime_pruned_calls"
_WA_PRUNED_TOKENS_KEY = "wa_alltime_pruned_tokens"


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _get_setting_float(conn, key: str, default: float = 0.0) -> float:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    try:
        return float(row[0]) if row else default
    except (TypeError, ValueError):
        return default


def _add_setting_float(conn, key: str, delta: float) -> None:
    current = _get_setting_float(conn, key)
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, str(current + delta)),
    )


def whatsapp_pruned_totals() -> dict:
    """The persisted running total of WhatsApp cost/calls/tokens that have
    already been pruned out of usage_logs. budget_tracker.py adds this on
    top of its live scan over whatever rows remain, so the 'all-time'
    WhatsApp stat stays accurate even after old rows are deleted."""
    conn = get_connection()
    try:
        return {
            "cost": _get_setting_float(conn, _WA_PRUNED_COST_KEY, 0.0),
            "calls": int(_get_setting_float(conn, _WA_PRUNED_CALLS_KEY, 0.0)),
            "tokens": int(_get_setting_float(conn, _WA_PRUNED_TOKENS_KEY, 0.0)),
        }
    finally:
        conn.close()


def _prune_usage_logs(*, dry_run: bool, days: int = USAGE_LOGS_RETENTION_DAYS) -> dict:
    """usage_logs has no real timestamp COLUMN -- each row is a JSON blob
    with the timestamp embedded inside (see budget_tracker.record_usage's
    entry shape). ISO-8601 strings sort correctly as plain text, so a
    string comparison against the cutoff is exact, no parsing needed for
    the comparison itself -- only for pulling out the WhatsApp fields to
    fold forward before deleting."""
    cutoff = _cutoff_iso(days)
    conn = get_connection()
    try:
        rows = conn.execute("SELECT rowid, data FROM usage_logs").fetchall()
        to_delete = []
        wa_cost = wa_calls = wa_tokens = 0
        for rowid, data_str in rows:
            try:
                log = json.loads(data_str)
            except Exception:
                continue
            ts = str(log.get("timestamp") or "")
            if ts and ts >= cutoff:
                continue  # recent enough, keep
            if not ts:
                continue  # never guess-delete a row with no timestamp at all
            to_delete.append(rowid)
            if log.get("task_type") == "whatsapp":
                wa_calls += 1
                wa_cost += float(log.get("cost_usd", 0.0) or 0.0)
                wa_tokens += int(log.get("input_tokens", 0) or 0) + int(log.get("output_tokens", 0) or 0)

        if to_delete and not dry_run:
            with conn:
                if wa_calls:
                    _add_setting_float(conn, _WA_PRUNED_COST_KEY, wa_cost)
                    _add_setting_float(conn, _WA_PRUNED_CALLS_KEY, wa_calls)
                    _add_setting_float(conn, _WA_PRUNED_TOKENS_KEY, wa_tokens)
                placeholders = ",".join("?" * len(to_delete))
                conn.execute(f"DELETE FROM usage_logs WHERE rowid IN ({placeholders})",
                            to_delete)
        return {"table": "usage_logs", "would_delete" if dry_run else "deleted": len(to_delete),
                "whatsapp_rows_folded": wa_calls}
    except Exception:
        logger.exception("data_retention: usage_logs prune failed")
        return {"table": "usage_logs", "error": "failed"}
    finally:
        conn.close()


def _prune_simple(table: str, date_col: str, *, dry_run: bool, days: int,
                  extra_where: str = "") -> dict:
    """Shared path for tables with a real indexed timestamp column."""
    cutoff = _cutoff_iso(days)
    conn = get_connection()
    try:
        where = f"{date_col} < ?" + (f" AND {extra_where}" if extra_where else "")
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", (cutoff,)
        ).fetchone()[0]
        if count and not dry_run:
            with conn:
                conn.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,))
        return {"table": table, "would_delete" if dry_run else "deleted": count}
    except Exception:
        logger.exception("data_retention: %s prune failed", table)
        return {"table": table, "error": "failed"}
    finally:
        conn.close()


SHEET_EDIT_LOG_BATCH_SIZE = 2000


def _prune_sheet_edit_log(*, dry_run: bool, keep_per_task: int = SHEET_EDIT_LOG_KEEP_PER_TASK) -> dict:
    """Caps sheet_edit_log to the most recent `keep_per_task` versions PER
    TASK (see the module docstring for why this is a per-task cap and not
    an age cutoff). A task with fewer versions than the cap is completely
    untouched -- this only ever removes the *excess* beyond what any
    realistic Restore use would need. Uses a window function (ROW_NUMBER,
    SQLite 3.25+, bundled with Python 3.11 for years) to rank each task's
    own versions newest-first, then deletes anything past the cap in small
    batches (SHEET_EDIT_LOG_BATCH_SIZE), checkpointing the WAL after each
    one.

    Batched on purpose, learned the hard way (CLAUDE.md gotcha, 2026-09-28
    round 2): a first version of this did the whole delete as ONE
    transaction. On a near-full volume, that grew app.db-wal to 61MB before
    the transaction failed -- and a failed/rolled-back transaction does NOT
    shrink the WAL file back down by itself, so the failure left the disk
    just as full as before, holding hostage space that had just been freed
    for exactly this purpose. Deleting a few thousand rows at a time and
    checkpointing between batches keeps the WAL's peak size bounded to one
    batch's worth of change, regardless of how many rows need deleting in
    total."""
    conn = get_connection()
    try:
        rank_sql = """SELECT id, ROW_NUMBER() OVER (
                          PARTITION BY task_id ORDER BY edited_at DESC, id DESC
                      ) AS rn FROM sheet_edit_log"""
        ids_to_delete = [r[0] for r in conn.execute(
            f"SELECT id FROM ({rank_sql}) WHERE rn > ?", (keep_per_task,)
        ).fetchall()]
        count = len(ids_to_delete)
        if count and not dry_run:
            for i in range(0, count, SHEET_EDIT_LOG_BATCH_SIZE):
                batch = ids_to_delete[i:i + SHEET_EDIT_LOG_BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                with conn:
                    conn.execute(f"DELETE FROM sheet_edit_log WHERE id IN ({placeholders})", batch)
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
        return {"table": "sheet_edit_log", "would_delete" if dry_run else "deleted": count,
                "keep_per_task": keep_per_task}
    except Exception:
        logger.exception("data_retention: sheet_edit_log prune failed")
        return {"table": "sheet_edit_log", "error": "failed"}
    finally:
        conn.close()


def _real_task_ids_for_client(client_id: str):
    """Real, currently-existing task ids for one client (Notion page ids, or
    local SQLite `tasks` ids) -- or None if this can't be reliably
    determined. Checks google_sheet_links.is_notion if the client is (or
    was) linked, to pin down which store to trust; otherwise tries Notion
    first (the more common mode in this app), then the local SQLite `tasks`
    table. Never raises -- any failure anywhere in here means None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_notion FROM google_sheet_links WHERE client_id=?", (client_id,)
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    is_notion_hint = bool(row[0]) if row else None

    def _try_notion():
        try:
            import notion_store
            if not notion_store.is_configured():
                return None
            tasks = notion_store.list_tasks(client_notion_id=client_id)
            return {t.get("notion_id") for t in tasks if t.get("notion_id")}
        except Exception:
            logger.exception("data_retention: Notion task lookup failed for client %s", client_id)
            return None

    def _try_sqlite():
        try:
            conn2 = get_connection()
            rows = conn2.execute("SELECT id FROM tasks WHERE client_id=?", (client_id,)).fetchall()
            conn2.close()
            return {str(r[0]) for r in rows}
        except Exception:
            return None

    if is_notion_hint is True:
        return _try_notion()
    if is_notion_hint is False:
        return _try_sqlite()
    return _try_notion() or _try_sqlite()


def _prune_orphaned_sheet_edit_log(*, dry_run: bool) -> dict:
    """Deletes sheet_edit_log rows for a task_id that no longer exists as a
    real task at all, checked LIVE per client (see
    _real_task_ids_for_client). Targets exactly the garbage a create-then-
    delete sync churn leaves behind -- CLAUDE.md gotcha, 2026-09-28 round 2:
    one client's runaway Google Sheets sync logged ~194,600 distinct
    phantom task-creation events in a two-day burst, of which only 112
    tasks still exist for real -- without touching a single row of history
    for any task that's still real. _prune_sheet_edit_log's per-task cap
    can't help with this specific shape of bloat (many distinct tasks with
    1-2 rows each, not one task with many rows), which is why this exists
    as a separate, complementary rule.

    Deliberately conservative: a client whose real-task lookup can't be
    confirmed, or comes back completely empty, is SKIPPED ENTIRELY for that
    client -- this must never be the thing that decides "this client has
    zero real tasks" off an ambiguous or failed live check (same philosophy
    as the Google Sheets sync's own empty-snapshot safety guard, gotcha
    #87). A client with real tasks only ever loses history for task_ids
    that are provably gone."""
    conn = get_connection()
    try:
        client_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT client_id FROM sheet_edit_log"
        ).fetchall()]
        total = 0
        by_client = []
        for client_id in client_ids:
            real_ids = _real_task_ids_for_client(client_id)
            if not real_ids:
                by_client.append({"client_id": client_id, "skipped": True,
                                  "reason": "no confirmed real tasks -- never guess-delete"})
                continue
            logged_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT task_id FROM sheet_edit_log WHERE client_id=?", (client_id,)
            ).fetchall()]
            orphaned = [t for t in logged_ids if t not in real_ids]
            if not orphaned:
                continue
            ids_to_delete = []
            for i in range(0, len(orphaned), 500):
                chunk = orphaned[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT id FROM sheet_edit_log WHERE client_id=? AND task_id IN ({placeholders})",
                    [client_id] + chunk,
                ).fetchall()
                ids_to_delete.extend(r[0] for r in rows)
            count = len(ids_to_delete)
            if count and not dry_run:
                for i in range(0, count, SHEET_EDIT_LOG_BATCH_SIZE):
                    batch = ids_to_delete[i:i + SHEET_EDIT_LOG_BATCH_SIZE]
                    placeholders = ",".join("?" * len(batch))
                    with conn:
                        conn.execute(f"DELETE FROM sheet_edit_log WHERE id IN ({placeholders})", batch)
                    try:
                        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception:
                        pass
            total += count
            count_key = "rows_would_delete" if dry_run else "rows_deleted"
            by_client.append({"client_id": client_id, "orphaned_task_ids": len(orphaned), count_key: count})
        return {"table": "sheet_edit_log_orphans", "would_delete" if dry_run else "deleted": total,
                "by_client": by_client}
    except Exception:
        logger.exception("data_retention: orphaned sheet_edit_log prune failed")
        return {"table": "sheet_edit_log_orphans", "error": "failed"}
    finally:
        conn.close()


def run_retention_sweep(*, dry_run: bool = False) -> dict:
    """Run every retention rule. Each is independent -- one failing never
    blocks the others. Safe to call as often as you like; a table with
    nothing old to prune is just a fast no-op."""
    results = [
        _prune_usage_logs(dry_run=dry_run),
        _prune_simple("wa_action_log", "created_at", dry_run=dry_run,
                     days=WA_ACTION_LOG_RETENTION_DAYS),
        _prune_simple("wa_call_outbox", "created_at", dry_run=dry_run,
                     days=WA_CALL_OUTBOX_RETENTION_DAYS,
                     extra_where="status != 'pending'"),
        _prune_sheet_edit_log(dry_run=dry_run),
        _prune_orphaned_sheet_edit_log(dry_run=dry_run),
    ]
    return {"dry_run": dry_run, "results": results}
