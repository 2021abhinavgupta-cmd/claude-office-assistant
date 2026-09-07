"""
error_monitoring.py — optional Sentry-protocol error tracking (Sentry cloud
or a self-hosted GlitchTip instance — same DSN-based `sentry-sdk` code
works against either, GlitchTip implements the same ingest protocol).

Why this exists
----------------
This codebase's own CLAUDE.md is a long history of bugs that shipped and
sat silently 500ing in production until a person happened to notice and
report it (the recurring "variable set in one if/else branch, read
unconditionally after" class alone accounts for a dozen+ gotchas). Right
now the only visibility into a production exception is `logs/app.log` on
the Railway volume — nobody is watching it proactively. This module adds
automatic capture + alerting for every unhandled exception, at zero cost
for a 10-person internal tool's actual error volume.

Same contract as this codebase's other optional layers (semantic_kb.py,
smart_memory.py): OFF until explicitly configured, a guarded import so a
server that never installed the dependency keeps running completely
normally, one env var to turn on.

Turning it on
-------------
  1. Add the one line from backend/requirements-monitoring.txt to the root
     requirements.txt and redeploy (this is the only step with any deploy
     risk — watch that deploy, same convention as every other optional
     dependency in this codebase).
  2. Set SENTRY_DSN on Railway to either:
       - a free Sentry.io cloud project's DSN (5,000 errors/month free,
         zero infra to run yourself), or
       - your self-hosted GlitchTip instance's DSN (see
         deploy/glitchtip/docker-compose.yml — no per-event cap, but you
         run the server: Postgres + GlitchTip web/worker, ~256-512MB RAM).
  3. Redeploy. Every unhandled exception from here on is captured
     automatically via Flask's error-signal integration — no code changes
     needed anywhere else in the app.

Until SENTRY_DSN is set, init_error_monitoring() is a no-op and nothing
about how this app behaves changes.
"""
import logging
import os

logger = logging.getLogger(__name__)


def init_error_monitoring() -> bool:
    """Call once, early, before the Flask app starts serving requests
    (see app.py). Returns True if monitoring is actually active."""
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            # Errors only, no performance/trace sampling -- this app's own
            # budget/usage tracking already covers cost and latency; the
            # point of this module is exception visibility, and keeping
            # trace volume at zero also keeps well clear of Sentry's free
            # tier's event cap.
            traces_sample_rate=0.0,
            environment=os.getenv("SENTRY_ENVIRONMENT",
                                  "production" if os.getenv("FLASK_ENV", "development") != "development"
                                  else "development"),
            release=os.getenv("SENTRY_RELEASE", "") or None,
            # Don't let a malformed/oversized error payload become its own
            # 500 -- this must never be the thing that breaks a request.
            max_breadcrumbs=50,
        )
        logger.info("error_monitoring: active (SENTRY_DSN set)")
        return True
    except Exception:
        logger.warning(
            "error_monitoring: SENTRY_DSN is set but sentry-sdk isn't "
            "installed (or init failed) -- add the line from "
            "backend/requirements-monitoring.txt to requirements.txt and "
            "redeploy. The app runs completely normally without it.",
            exc_info=True,
        )
        return False
