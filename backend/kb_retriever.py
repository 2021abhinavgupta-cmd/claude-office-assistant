"""
KB Retriever — lightweight Claude-Projects-like retrieval for Project Knowledge Base.

- Chunks docs and indexes them in SQLite FTS5
- Retrieves only the most relevant chunks per user message
"""

from __future__ import annotations

import re
import logging
import os
import json
import math
from typing import List, Dict, Optional

from db import get_connection

logger = logging.getLogger(__name__)


def _normalize_query(q: str) -> str:
    q = " ".join((q or "").split())
    # FTS5 special chars can break queries; keep it simple
    q = re.sub(r"[^\w\s\-:/\.]", " ", q)
    q = " ".join(q.split())
    return q[:400]


def chunk_text(text: str, *, max_chars: int = 900, overlap: int = 120) -> List[str]:
    t = " ".join((text or "").split())
    if not t:
        return []
    if max_chars <= 0:
        return [t]

    chunks = []
    i = 0
    n = len(t)
    while i < n:
        end = min(i + max_chars, n)
        chunk = t[i:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return chunks


def index_doc(project_id: str, user_id: str, doc_id: str, filename: str, content: str) -> None:
    """
    (Re)index a single KB doc. Safe to call repeatedly.
    """
    try:
        chunks = chunk_text(content)
        conn = get_connection()
        with conn:
            # Remove old chunks for this doc
            conn.execute(
                "DELETE FROM kb_chunks_fts WHERE project_id=? AND user_id=? AND doc_id=?",
                (project_id, user_id, doc_id),
            )
            conn.execute(
                "DELETE FROM kb_vectors WHERE project_id=? AND user_id=? AND doc_id=?",
                (project_id, user_id, doc_id),
            )
            for c in chunks:
                conn.execute(
                    "INSERT INTO kb_chunks_fts (project_id, user_id, doc_id, filename, chunk) VALUES (?, ?, ?, ?, ?)",
                    (project_id, user_id, doc_id, filename, c),
                )
        conn.close()

        # Semantic index (optional). If OPENAI_API_KEY isn't set, skip silently.
        if _openai_available():
            _index_vectors(project_id, user_id, doc_id, filename, chunks)
    except Exception as e:
        logger.warning(f"KB index failed (non-fatal): {e}")


def delete_doc_index(project_id: str, user_id: str, doc_id: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "DELETE FROM kb_chunks_fts WHERE project_id=? AND user_id=? AND doc_id=?",
                (project_id, user_id, doc_id),
            )
            conn.execute(
                "DELETE FROM kb_vectors WHERE project_id=? AND user_id=? AND doc_id=?",
                (project_id, user_id, doc_id),
            )
        conn.close()
    except Exception as e:
        logger.warning(f"KB deindex failed (non-fatal): {e}")


def search(project_id: str, user_id: str, query: str, *, limit: int = 6) -> List[Dict]:
    """
    Return top matching chunks for a project/user.
    """
    # Prefer semantic retrieval when configured; fall back to FTS.
    if _openai_available():
        sem = _semantic_search(project_id, user_id, query, limit=limit)
        if sem:
            return sem

    q = _normalize_query(query)
    if not q:
        return []

    try:
        conn = get_connection()
        cur = conn.cursor()
        # FTS5 supports bm25() for ranking
        cur.execute(
            """
            SELECT filename, doc_id, chunk, bm25(kb_chunks_fts) AS score
            FROM kb_chunks_fts
            WHERE project_id=? AND user_id=? AND kb_chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (project_id, user_id, q, int(limit)),
        )
        rows = cur.fetchall()
        conn.close()
        return [{"filename": r[0], "doc_id": r[1], "chunk": r[2], "score": r[3]} for r in rows]
    except Exception as e:
        logger.warning(f"KB search failed (non-fatal): {e}")
        return []


def format_for_prompt(matches: List[Dict]) -> Optional[str]:
    if not matches:
        return None

    lines = ["## Project Knowledge Base (Relevant Excerpts)"]
    by_doc = {}
    for m in matches:
        key = (m.get("doc_id"), m.get("filename"))
        by_doc.setdefault(key, []).append(m.get("chunk", ""))

    for (_, filename), chunks in by_doc.items():
        lines.append(f"\n### {filename}")
        for c in chunks[:3]:
            c = " ".join((c or "").split())
            if c:
                lines.append(f"- {c}")
    return "\n".join(lines).strip()


def _openai_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _get_openai_client():
    # Lazy import so the app runs without OpenAI installed/configured.
    try:
        from openai import OpenAI
        return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return None


def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    client = _get_openai_client()
    if not client:
        return None
    try:
        resp = client.embeddings.create(
            model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            input=texts,
        )
        return [d.embedding for d in resp.data]
    except Exception as e:
        logger.warning(f"Embeddings failed (non-fatal): {e}")
        return None


def _index_vectors(project_id: str, user_id: str, doc_id: str, filename: str, chunks: List[str]) -> None:
    if not chunks:
        return
    embs = _embed_texts(chunks)
    if not embs:
        return
    try:
        conn = get_connection()
        with conn:
            for chunk, emb in zip(chunks, embs):
                conn.execute(
                    "INSERT INTO kb_vectors (project_id, user_id, doc_id, filename, chunk, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, user_id, doc_id, filename, chunk, json.dumps(emb)),
                )
        conn.close()
    except Exception as e:
        logger.warning(f"Vector index write failed (non-fatal): {e}")


def _cosine(a: List[float], b: List[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _semantic_search(project_id: str, user_id: str, query: str, *, limit: int = 6) -> List[Dict]:
    q = " ".join((query or "").split())
    if not q:
        return []

    qembs = _embed_texts([q])
    if not qembs:
        return []
    qemb = qembs[0]

    # Pull a bounded set of candidate chunks to score in Python.
    # This keeps it simple and reliable without external vector DBs.
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT filename, doc_id, chunk, embedding
            FROM kb_vectors
            WHERE project_id=? AND user_id=?
            ORDER BY id DESC
            LIMIT 400
            """,
            (project_id, user_id),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"Vector read failed (non-fatal): {e}")
        return []

    scored = []
    for filename, doc_id, chunk, emb_json in rows:
        try:
            emb = json.loads(emb_json)
            score = _cosine(qemb, emb)
            scored.append((score, filename, doc_id, chunk))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, filename, doc_id, chunk in scored[: int(limit)]:
        out.append({"filename": filename, "doc_id": doc_id, "chunk": chunk, "score": score})
    return out

