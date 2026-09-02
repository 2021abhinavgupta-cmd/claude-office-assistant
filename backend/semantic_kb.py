"""
semantic_kb.py — optional semantic (embedding) layer over the FTS5 knowledge base.

Why this exists
---------------
The keyword index (`kb_chunks_fts`, bm25) only matches on shared words: a question
about "refunds" won't surface a doc that only ever says "returns & cancellations".
This module adds a meaning-based match on top. It is:

  * OFF by default          — nothing runs until an admin flips `kb_semantic_enabled`
  * fully self-contained     — one small static-embedding model, vectors in SQLite
  * a pure add-on            — callers always keep their bm25 results; semantic hits
                               are merged in, never replace. Any failure → silent
                               fall back to keyword-only.
  * zero extra API keys/cost — embeddings run locally (model2vec, CPU, ~30 MB model)

Turning it on (Railway)
-----------------------
  1. add the two lines from  backend/requirements-semantic.txt  to requirements.txt
     and redeploy (this is the only step with any deploy risk — watch that deploy)
  2. POST /api/kb/semantic  {"enabled": true, "user_id": "<emp>"}   (kicks a backfill)

Until step 1 lands, `available()` is False and every function here no-ops, so
merging/deploying this file on its own changes nothing about the running app.
"""

from __future__ import annotations

import os
import logging
import threading

from db import get_connection, DB_PATH

logger = logging.getLogger(__name__)

# Keep any Hugging Face model download on the persistent volume (next to app.db),
# not the ephemeral container FS — otherwise it re-downloads on every redeploy.
os.environ.setdefault("HF_HOME", os.path.join(os.path.dirname(DB_PATH), "hf_cache"))

_MODEL_ID = os.getenv("KB_SEMANTIC_MODEL", "minishlab/potion-base-8M")
_SETTING_KEY = "kb_semantic_enabled"

_model = None            # lazy singleton; False once we know it can't load
_model_lock = threading.Lock()
_backfill = {"running": False, "done": 0, "total": 0, "error": ""}


# ── availability / enablement ───────────────────────────────────────────────

def _load_model():
    """Return the embedding model, or None if deps/model aren't available.
    Result is cached (including the failure) for the process lifetime."""
    global _model
    if _model is not None:
        return _model or None
    with _model_lock:
        if _model is not None:
            return _model or None
        try:
            import importlib
            importlib.import_module("numpy")   # needed downstream; fail fast here
            from model2vec import StaticModel
            _model = StaticModel.from_pretrained(_MODEL_ID)
            logger.info("semantic_kb: loaded %s", _MODEL_ID)
        except Exception as e:
            _model = False
            logger.warning("semantic_kb: unavailable (%s) — keyword search only", e)
    return _model or None


def available() -> bool:
    """True when the model + numpy are importable and the model loaded."""
    return _load_model() is not None


def is_enabled() -> bool:
    """Env var wins if set; otherwise the stored app_settings flag (default off)."""
    env = (os.getenv("KB_SEMANTIC_ENABLED") or "").strip().lower()
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
        logger.exception("semantic_kb: set_enabled failed")


def _active() -> bool:
    return is_enabled() and available()


# ── embedding ──────────────────────────────────────────────────────────────

def _embed(texts: list) -> "list | None":
    """Batch-embed to unit-norm float32 rows. None on any failure."""
    model = _load_model()
    if model is None or not texts:
        return None
    try:
        import numpy as np
        vecs = np.asarray(model.encode(list(texts)), dtype=np.float32)
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms
    except Exception:
        logger.debug("semantic_kb: embed failed", exc_info=True)
        return None


# ── indexing (called from kb_retriever after it writes the FTS rows) ────────

def index_doc(doc_id: str) -> None:
    """(Re)embed every current chunk of one doc. No-op unless active."""
    if not doc_id or not _active():
        return
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT rowid, chunk FROM kb_chunks_fts WHERE doc_id=?", (doc_id,)
        ).fetchall()
        if rows:
            embs = _embed([r[1] for r in rows])
            if embs is not None:
                dim = int(embs.shape[1])
                with conn:
                    conn.execute(
                        "DELETE FROM kb_chunk_vectors WHERE fts_rowid IN "
                        "(SELECT rowid FROM kb_chunks_fts WHERE doc_id=?)", (doc_id,)
                    )
                    conn.executemany(
                        "INSERT OR REPLACE INTO kb_chunk_vectors (fts_rowid, dim, vec) "
                        "VALUES (?, ?, ?)",
                        [(int(r[0]), dim, embs[i].tobytes()) for i, r in enumerate(rows)],
                    )
        conn.close()
        prune()
    except Exception:
        logger.debug("semantic_kb: index_doc failed", exc_info=True)


def prune() -> None:
    """Drop vectors whose FTS chunk is gone (doc deleted / re-chunked)."""
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "DELETE FROM kb_chunk_vectors WHERE fts_rowid NOT IN "
                "(SELECT rowid FROM kb_chunks_fts)"
            )
        conn.close()
    except Exception:
        logger.debug("semantic_kb: prune failed", exc_info=True)


# ── search ─────────────────────────────────────────────────────────────────

def search(query: str, *, limit: int = 6,
           project_id: "str | None" = None, user_id: "str | None" = None) -> list:
    """Top chunks by cosine similarity. [] unless active. Never raises.

    Returns dicts shaped like kb_retriever.search():
      {filename, doc_id, chunk, score}   — score is cosine sim in [-1, 1], higher = better
    """
    if not query or not _active():
        return []
    qv = _embed([query])
    if qv is None:
        return []
    try:
        import numpy as np
        qv = qv[0]
        conn = get_connection()

        allowed = None
        if project_id is not None or user_id is not None:
            allowed = {
                int(r[0]) for r in conn.execute(
                    "SELECT rowid FROM kb_chunks_fts WHERE project_id=? AND user_id=?",
                    (project_id or "", user_id or ""),
                ).fetchall()
            }
            if not allowed:
                conn.close()
                return []

        vrows = conn.execute(
            "SELECT fts_rowid, dim, vec FROM kb_chunk_vectors"
        ).fetchall()
        if not vrows:
            conn.close()
            return []

        want_dim = int(qv.shape[0])
        ids, blobs = [], []
        for rid, dim, blob in vrows:
            if int(dim) != want_dim:
                continue
            if allowed is not None and int(rid) not in allowed:
                continue
            ids.append(int(rid))
            blobs.append(blob)
        if not ids:
            conn.close()
            return []

        mat = np.vstack([np.frombuffer(b, dtype=np.float32) for b in blobs])
        scores = mat @ qv
        k = min(int(limit), len(ids))
        top = np.argsort(-scores)[:k]
        top_ids = [ids[i] for i in top]
        score_by_id = {ids[i]: float(scores[i]) for i in top}

        placeholders = ",".join("?" * len(top_ids))
        meta = conn.execute(
            f"SELECT rowid, filename, doc_id, chunk FROM kb_chunks_fts "
            f"WHERE rowid IN ({placeholders})", top_ids
        ).fetchall()
        conn.close()

        out = [
            {"filename": m[1], "doc_id": m[2], "chunk": m[3],
             "score": score_by_id.get(int(m[0]), 0.0)}
            for m in meta
        ]
        out.sort(key=lambda d: d["score"], reverse=True)
        return out
    except Exception:
        logger.debug("semantic_kb: search failed", exc_info=True)
        return []


# ── backfill (embed everything already in the FTS index) ────────────────────

def _backfill_worker(batch: int = 256) -> None:
    global _backfill
    try:
        if not available():
            _backfill = {"running": False, "done": 0, "total": 0,
                         "error": "embedding deps not installed"}
            return
        conn = get_connection()
        todo = conn.execute(
            "SELECT rowid, chunk FROM kb_chunks_fts WHERE rowid NOT IN "
            "(SELECT fts_rowid FROM kb_chunk_vectors)"
        ).fetchall()
        conn.close()

        _backfill = {"running": True, "done": 0, "total": len(todo), "error": ""}
        for i in range(0, len(todo), batch):
            group = todo[i:i + batch]
            embs = _embed([r[1] for r in group])
            if embs is None:
                _backfill["error"] = "embed failed mid-run"
                break
            dim = int(embs.shape[1])
            conn = get_connection()
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO kb_chunk_vectors (fts_rowid, dim, vec) "
                    "VALUES (?, ?, ?)",
                    [(int(r[0]), dim, embs[j].tobytes()) for j, r in enumerate(group)],
                )
            conn.close()
            _backfill["done"] += len(group)
        prune()
    except Exception as e:
        _backfill["error"] = str(e)
        logger.exception("semantic_kb: backfill failed")
    finally:
        _backfill["running"] = False


def start_backfill() -> dict:
    """Kick a background backfill if one isn't already running."""
    if _backfill["running"]:
        return dict(_backfill)
    if not available():
        return {"running": False, "done": 0, "total": 0,
                "error": "embedding deps not installed"}
    threading.Thread(target=_backfill_worker, daemon=True).start()
    return {"running": True, "done": 0, "total": 0, "error": ""}


# ── status ─────────────────────────────────────────────────────────────────

def stats() -> dict:
    vectors = fts_chunks = 0
    try:
        conn = get_connection()
        vectors = conn.execute("SELECT COUNT(*) FROM kb_chunk_vectors").fetchone()[0]
        fts_chunks = conn.execute("SELECT COUNT(*) FROM kb_chunks_fts").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return {
        "available": available(),
        "enabled": is_enabled(),
        "active": _active(),
        "model": _MODEL_ID if available() else None,
        "vectors": vectors,
        "fts_chunks": fts_chunks,
        "backfill": dict(_backfill),
    }
