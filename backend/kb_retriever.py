"""
KB Retriever — lightweight Claude-Projects-like retrieval for Project Knowledge Base.

- Chunks docs and indexes them in SQLite FTS5
- Hybrid retrieval: primary token OR-query + secondary AND-query for precision
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


def _fts_query(q: str) -> str:
    """
    Turn a user query into a forgiving FTS5 MATCH expression.
    - Uses OR between tokens so partial matches still retrieve context.
    - Quotes long-ish tokens to prefer phrase-like matches.
    """
    qn = _normalize_query(q)
    if not qn:
        return ""
    toks = [t for t in qn.split() if len(t) >= 2]
    if not toks:
        return ""
    parts = []
    for t in toks[:24]:
        if len(t) >= 6:
            parts.append(f"\"{t}\"")
        else:
            parts.append(t)
    return " OR ".join(parts)


def _keyword_and_query(q: str) -> str:
    """Stricter AND query over longer tokens — boosts precision when OR is too noisy."""
    qn = _normalize_query(q)
    toks = [t for t in qn.split() if len(t) >= 4][:6]
    if len(toks) < 2:
        return ""
    return " AND ".join(f'"{t}"' for t in toks)


def chunk_text(text: str, *, max_chars: int = 1000, overlap: int = 180) -> List[str]:
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
    mq = _fts_query(query)
    if not mq:
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
            (project_id, user_id, mq, int(limit)),
        )
        rows = cur.fetchall()
        conn.close()
        return [{"filename": r[0], "doc_id": r[1], "chunk": r[2], "score": r[3]} for r in rows]
    except Exception as e:
        logger.warning(f"KB search failed (non-fatal): {e}")
        return []


def _merge_ranked_chunks(primary: List[Dict], secondary: List[Dict], *, limit: int) -> List[Dict]:
    """Merge two ranked lists; bm25 score — lower is better in SQLite FTS5."""
    seen: set = set()
    merged: List[Dict] = []
    for m in primary + secondary:
        key = (m.get("doc_id"), (m.get("chunk") or "")[:140])
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)
    merged.sort(key=lambda x: float(x.get("score") or 0.0))
    return merged[: int(limit)]


def search_hybrid(project_id: str, user_id: str, query: str, *, limit: int = 8) -> List[Dict]:
    """
    OR-query recall + optional AND-query precision, merged and re-ranked.
    """
    primary = search(project_id, user_id, query, limit=max(limit, 6))
    and_q = _keyword_and_query(query)
    or_q = _fts_query(query)
    secondary: List[Dict] = []
    if and_q and and_q.strip() != (or_q or "").strip():
        secondary = search(project_id, user_id, and_q, limit=max(4, limit // 2))
    return _merge_ranked_chunks(primary, secondary, limit=limit)


def unique_doc_labels(matches: List[Dict]) -> List[Dict]:
    """Unique (doc_id, filename) for UI attribution."""
    out: List[Dict] = []
    seen = set()
    for m in matches or []:
        key = (m.get("doc_id"), m.get("filename"))
        if not (key[0] or key[1]):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({"filename": m.get("filename") or "", "doc_id": m.get("doc_id") or ""})
    return out


def format_for_prompt(matches: List[Dict]) -> Optional[str]:
    if not matches:
        return None

    lines = [
        "## Project Knowledge Base (Relevant Excerpts)",
        "When you use a fact from this KB, cite it inline as **[KB: *filename*]** (use the exact filename below).",
        "If you quote, keep quotes under ~25 words and only when necessary for accuracy.",
    ]
    by_doc: Dict = {}
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
