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

Deliberately NOT touched here, even though they can grow large:
  sheet_edit_log  -- backs the user-facing Version History / Restore
                     feature (CLAUDE.md gotchas #74/#75) -- pruning here
                     means someone can no longer restore an old version.
                     Left for a human decision, not an automatic one.
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
    ]
    return {"dry_run": dry_run, "results": results}
