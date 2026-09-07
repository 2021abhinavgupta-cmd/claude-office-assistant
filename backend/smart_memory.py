"""
smart_memory.py — optional semantic (embedding) layer over per-user memory.

Why this exists
----------------
memory_store.py is a flat, per-user JSON list: every save is a blind append,
and every read (`format_for_prompt`) just takes the most recent 20 items in
storage order. That's fine at a handful of memories, but it means (a) the
same fact can get saved as a near-duplicate over and over with nothing ever
deduping it, and (b) once a user has more than ~20 memories, the ones
actually relevant to *this* message can silently fall off the end in favour
of whatever was saved most recently, regardless of relevance.

This module adds the two things a "Mem0-style" memory layer is actually
valued for, without adopting Mem0 itself (which needs its own Postgres +
Neo4j + FastAPI server stack — real infra this app has deliberately avoided
everywhere, see semantic_kb.py's own reasoning for the same tradeoff):

  * semantic recall  — format_for_prompt(user_id, query) ranks a user's
                        memories by relevance to the CURRENT message instead
                        of just returning the most recently saved ones.
  * light dedup       — dedupe_or_save() skips saving a new memory that's
                        near-identical (cosine >= _DUP_THRESHOLD) to one the
                        user already has, instead of appending a duplicate
                        forever.

Same contract as semantic_kb.py, deliberately:
  * OFF by default          — nothing runs until an admin flips
                               `memory_smart_enabled`
  * a pure add-on            — every function here either improves on
                               memory_store.py's existing behaviour or, when
                               inactive/on any failure, falls back to it
                               UNCHANGED. Never worse, never required.
  * zero extra API keys/cost — reuses semantic_kb's already-loaded embedder
                               (model2vec, CPU, local) rather than loading a
                               second copy of the model or calling an LLM.

Auto-extraction (the other half of "Mem0-style") is deliberately NOT built
here as a separate LLM call — that would add a real per-message API cost,
which cuts against the cost-reduction work done alongside this feature. The
main chat already extracts facts for free by having the model emit a
<SAVE_MEMORY_PROFILE> tag on its own regular response (see app.py's stream
handler) with no extra round trip; the WhatsApp agent's <REMEMBER> tag
(whatsapp_agent.py) follows the identical zero-extra-cost pattern. This
module just makes what gets saved that way easier to dedupe and easier to
recall well.
"""

from __future__ import annotations

import logging
import threading

from db import get_connection
import memory_store
import semantic_kb

logger = logging.getLogger(__name__)

_SETTING_KEY = "memory_smart_enabled"
# Cosine sim above which a new memory is treated as a duplicate of an
# existing one. Calibrated empirically against the real embedder
# (model2vec/potion-base-8M, the same one semantic_kb.py uses) -- a real
# near-duplicate pair ("Prefers concise email replies, no fluff" vs "Likes
# short, no-fluff email responses") scored ~0.77, while two genuinely
# distinct facts scored ~0.08-0.10. This model's cosine range runs much
# lower than a typical sentence-transformer's, so don't reuse a "0.9-ish"
# threshold from general ML intuition here -- 0.70 sits with real margin
# below the measured near-dup score and well above the distinct-fact noise
# floor. Re-measure if the embedder model ever changes.
_DUP_THRESHOLD = 0.70
_backfill = {"running": False, "done": 0, "total": 0, "error": ""}


# ── availability / enablement (identical shape to semantic_kb.py) ──────────

def available() -> bool:
    """True when the shared embedder (semantic_kb's) is importable/loaded."""
    return semantic_kb.available()


def is_enabled() -> bool:
    import os
    env = (os.getenv("MEMORY_SMART_ENABLED") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key=?", (_SETTING_KEY,)
        ).fetchone()
        conn.close()
        return bool(row) and str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def set_enabled(value: bool) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                (_SETTING_KEY, "1" if value else "0"),
            )
        conn.close()
    except Exception:
        logger.exception("smart_memory: set_enabled failed")


def _active() -> bool:
    return is_enabled() and available()


# ── indexing ─────────────────────────────────────────────────────────────

def index_memory(user_id: str, memory_id: str, content: str) -> None:
    """Embed and store one memory item's vector. No-op unless active."""
    if not (user_id and memory_id and content and _active()):
        return
    try:
        embs = semantic_kb.embed_texts([content])
        if embs is None:
            return
        dim = int(embs.shape[1])
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_vectors (user_id, memory_id, dim, vec) "
                "VALUES (?, ?, ?, ?)",
                (user_id, memory_id, dim, embs[0].tobytes()),
            )
        conn.close()
    except Exception:
        logger.debug("smart_memory: index_memory failed", exc_info=True)


def index_memories(user_id: str, mems: list) -> None:
    """Bulk version of index_memory — used after update_profile()'s
    replace-all-profile-entries save. No-op unless active."""
    if not (user_id and mems and _active()):
        return
    try:
        contents = [m.get("content", "") for m in mems if m.get("id")]
        ids = [m["id"] for m in mems if m.get("id")]
        if not ids:
            return
        embs = semantic_kb.embed_texts(contents)
        if embs is None:
            return
        dim = int(embs.shape[1])
        conn = get_connection()
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO memory_vectors (user_id, memory_id, dim, vec) "
                "VALUES (?, ?, ?, ?)",
                [(user_id, mid, dim, embs[i].tobytes()) for i, mid in enumerate(ids)],
            )
        conn.close()
    except Exception:
        logger.debug("smart_memory: index_memories failed", exc_info=True)


def remove_memory(user_id: str, memory_id: str) -> None:
    """Drop one memory's vector (call this from the delete-memory route)."""
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "DELETE FROM memory_vectors WHERE user_id=? AND memory_id=?",
                (user_id, memory_id),
            )
        conn.close()
    except Exception:
        logger.debug("smart_memory: remove_memory failed", exc_info=True)


def prune(user_id: str) -> None:
    """Drop vectors for memory items that no longer exist for this user
    (covers any deletion path that didn't call remove_memory directly,
    e.g. the MAX_PER_USER trim in memory_store.add_memory)."""
    try:
        live_ids = {m["id"] for m in memory_store.get_memories(user_id) if m.get("id")}
        conn = get_connection()
        rows = conn.execute(
            "SELECT memory_id FROM memory_vectors WHERE user_id=?", (user_id,)
        ).fetchall()
        stale = [r[0] for r in rows if r[0] not in live_ids]
        if stale:
            with conn:
                conn.executemany(
                    "DELETE FROM memory_vectors WHERE user_id=? AND memory_id=?",
                    [(user_id, mid) for mid in stale],
                )
        conn.close()
    except Exception:
        logger.debug("smart_memory: prune failed", exc_info=True)


# ── dedup on write ───────────────────────────────────────────────────────

def dedupe_or_save(user_id: str, content: str, source: str = "manual") -> dict:
    """Same contract as memory_store.add_memory(), except when active it
    first checks the new content against the user's existing memories and
    skips the save (returning the existing near-duplicate instead) if one
    is already >= _DUP_THRESHOLD similar. Falls straight through to a plain
    add_memory() when inactive or on any embedding failure."""
    content = (content or "").strip()
    if not content:
        return {}
    if not _active():
        return memory_store.add_memory(user_id, content, source)

    try:
        existing = memory_store.get_memories(user_id)
        if existing:
            qv = semantic_kb.embed_texts([content])
            if qv is not None:
                import numpy as np
                conn = get_connection()
                rows = conn.execute(
                    "SELECT memory_id, dim, vec FROM memory_vectors WHERE user_id=?",
                    (user_id,),
                ).fetchall()
                conn.close()
                want_dim = int(qv.shape[1])
                by_id = {m["id"]: m for m in existing}
                best_id, best_score = None, -1.0
                for mid, dim, blob in rows:
                    if int(dim) != want_dim or mid not in by_id:
                        continue
                    vec = np.frombuffer(blob, dtype=np.float32)
                    score = float(vec @ qv[0])
                    if score > best_score:
                        best_id, best_score = mid, score
                if best_id is not None and best_score >= _DUP_THRESHOLD:
                    logger.info("smart_memory: skipped near-duplicate memory "
                                "for %s (sim=%.3f)", user_id, best_score)
                    return by_id[best_id]
    except Exception:
        logger.debug("smart_memory: dedup check failed, saving anyway", exc_info=True)

    mem = memory_store.add_memory(user_id, content, source)
    if mem:
        index_memory(user_id, mem["id"], mem["content"])
        prune(user_id)   # covers the MAX_PER_USER trim silently dropping the oldest item
    return mem


# ── recall ───────────────────────────────────────────────────────────────

def recall(user_id: str, query: str, limit: int = 8) -> list:
    """Top memories for this user by relevance to `query`. [] on any
    failure or when inactive — callers should fall back to
    memory_store.format_for_prompt() in that case, see format_for_prompt()
    below which already does this."""
    if not (user_id and query and _active()):
        return []
    try:
        import numpy as np
        qv = semantic_kb.embed_texts([query])
        if qv is None:
            return []
        existing = {m["id"]: m for m in memory_store.get_memories(user_id) if m.get("id")}
        if not existing:
            return []
        conn = get_connection()
        rows = conn.execute(
            "SELECT memory_id, dim, vec FROM memory_vectors WHERE user_id=?",
            (user_id,),
        ).fetchall()
        conn.close()
        want_dim = int(qv.shape[1])
        scored = []
        for mid, dim, blob in rows:
            if int(dim) != want_dim or mid not in existing:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            scored.append((float(vec @ qv[0]), existing[mid]))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:limit]]
    except Exception:
        logger.debug("smart_memory: recall failed", exc_info=True)
        return []


def format_for_prompt(user_id: str, query: str = "") -> str:
    """Drop-in replacement for memory_store.format_for_prompt(). When smart
    recall is active and a query is given, ranks by relevance to that
    query; otherwise (inactive, no query, or a genuinely empty/failed
    recall) falls straight through to the original last-20 behaviour so
    this is never worse than before it existed."""
    if query and _active():
        top = recall(user_id, query, limit=8)
        if top:
            lines = "\n".join(f"• {m['content']}" for m in top)
            return f"\n\n## What you remember about this user (most relevant to this message):\n{lines}"
    return memory_store.format_for_prompt(user_id)


# ── backfill (embed every memory that predates this feature) ───────────────

def _backfill_worker() -> None:
    # Mutates the module-level dict's fields in place rather than rebinding
    # `_backfill` to a new dict object -- start_backfill() below already set
    # running=True synchronously before spawning this thread, and a caller
    # polling smart_memory._backfill (or stats()) needs to see that flip
    # immediately, not only once this thread gets scheduled. Rebinding here
    # would race a tight poll loop that checks right after start_backfill()
    # returns (a real bug this had at first, caught by a scratch test that
    # polled synchronously with no natural network-latency gap to hide it).
    try:
        if not available():
            _backfill["running"] = False
            _backfill["error"] = "embedding deps not installed"
            return
        conn = get_connection()
        user_ids = [r[0] for r in conn.execute("SELECT user_id FROM memory").fetchall()]
        conn.close()

        # Count total up front so progress is meaningful.
        todo = []
        for uid in user_ids:
            for m in memory_store.get_memories(uid):
                if m.get("id") and m.get("content"):
                    todo.append((uid, m["id"], m["content"]))

        _backfill["total"] = len(todo)
        _backfill["done"] = 0
        for uid, mid, content in todo:
            index_memory(uid, mid, content)
            _backfill["done"] += 1
        for uid in user_ids:
            prune(uid)
    except Exception as e:
        _backfill["error"] = str(e)
        logger.exception("smart_memory: backfill failed")
    finally:
        _backfill["running"] = False


def start_backfill() -> dict:
    """Kick a background backfill of every existing memory not yet
    vectorized. Idempotent (index_memory is INSERT OR REPLACE) so safe to
    call again after new memories are added. Sets running=True synchronously
    (before the thread starts) so a caller that polls immediately after this
    returns always sees a consistent in-progress state -- see the note in
    _backfill_worker above."""
    if _backfill["running"]:
        return dict(_backfill)
    if not available():
        return {"running": False, "done": 0, "total": 0,
                "error": "embedding deps not installed"}
    _backfill["running"] = True
    _backfill["done"] = 0
    _backfill["total"] = 0
    _backfill["error"] = ""
    threading.Thread(target=_backfill_worker, daemon=True).start()
    return dict(_backfill)


# ── status ───────────────────────────────────────────────────────────────

def stats() -> dict:
    vectors = 0
    try:
        conn = get_connection()
        vectors = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return {
        "available": available(),
        "enabled": is_enabled(),
        "active": _active(),
        "dup_threshold": _DUP_THRESHOLD,
        "vectors": vectors,
        "backfill": dict(_backfill),
    }
