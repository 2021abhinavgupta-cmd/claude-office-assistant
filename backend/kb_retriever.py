"""
KB Retriever — lightweight Claude-Projects-like retrieval for Project Knowledge Base.

- Chunks docs and indexes them in SQLite FTS5
- Retrieves only the most relevant chunks per user message
"""

from __future__ import annotations

import re
import logging
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
            for c in chunks:
                conn.execute(
                    "INSERT INTO kb_chunks_fts (project_id, user_id, doc_id, filename, chunk) VALUES (?, ?, ?, ?, ?)",
                    (project_id, user_id, doc_id, filename, c),
                )
        conn.close()
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
        conn.close()
    except Exception as e:
        logger.warning(f"KB deindex failed (non-fatal): {e}")


def search(project_id: str, user_id: str, query: str, *, limit: int = 6) -> List[Dict]:
    """
    Return top matching chunks for a project/user.
    """
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

