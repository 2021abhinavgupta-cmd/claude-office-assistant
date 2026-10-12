"""
web_search.py -- live web lookup for the WhatsApp assistant, on ANY model.

Anthropic's hosted web_search tool only exists for Claude, so while the bot runs
on Qwen it had no way to look anything up. `answer(query)` is the replacement
behind the `search_web` tool:

  1. OpenRouter's web plugin (needs OPENROUTER_API_KEY): one small call that
     searches the web and writes a short sourced answer. Costs a few cents per
     search (the plugin bills about $0.02 for 5 results) so it only runs when
     the bot decides it needs outside facts.
  2. Fallback: scrape DuckDuckGo's HTML results and hand the top snippets back
     for the main model to read.

Never raises; returns a plain string.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests

import llm_openrouter

logger = logging.getLogger(__name__)

_SEARCH_FEE = 0.02   # USD, approx. for the web plugin's 5 results
_SYS = ("You are a research helper. Use the web results to answer the question "
        "accurately and concisely (under 150 words). Name the source or site for "
        "key facts. Give dates and numbers exactly as found. If the results don't "
        "answer it, say so plainly instead of guessing.")


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _record(data: dict) -> None:
    try:
        from budget_tracker import record_usage
        from model_router import calculate_cost
        u = data.get("usage") or {}
        tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        record_usage(task_type="whatsapp", model_tier="qwen",
                     model_name=llm_openrouter.model_name() + " +web",
                     input_tokens=tin, output_tokens=tout,
                     cost=calculate_cost("qwen", tin, tout) + _SEARCH_FEE,
                     user_id="wa_web_search")
    except Exception:
        logger.debug("web_search: usage record failed", exc_info=True)


def _online_answer(query: str) -> str | None:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    payload = {
        "model": llm_openrouter.model_name(),
        "messages": [{"role": "system", "content": _SYS},
                     {"role": "user", "content": query}],
        "max_tokens": 600,
        "temperature": 0.2,
        "reasoning": {"enabled": False},
        "plugins": [{"id": "web", "max_results": 5}],
    }
    r = requests.post(llm_openrouter.API_URL, json=payload, timeout=50, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("PUBLIC_BASE_URL", "https://lumina.mmga.agency"),
        "X-Title": "Lumina"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:200])
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = re.sub(r"<think>[\s\S]*?</think>", "", str(msg.get("content") or "")).strip()
    if not text:
        return None
    _record(data)
    seen, names = set(), []
    for a in msg.get("annotations") or []:
        uc = (a or {}).get("url_citation") or {}
        d = _domain(uc.get("url", ""))
        if d and d not in seen:
            seen.add(d)
            names.append(d)
    return text + (f"\nSources: {', '.join(names[:4])}" if names else "")


def _ddg(query: str) -> str | None:
    from bs4 import BeautifulSoup
    r = requests.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; LuminaBot/1.0)"})
    if r.status_code >= 400:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select(".result")[:5]:
        a = res.select_one(".result__a")
        sn = res.select_one(".result__snippet")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        out.append(f"- {a.get_text(' ', strip=True)} ({_domain(href)}): "
                   f"{sn.get_text(' ', strip=True) if sn else ''}")
    return ("Web results:\n" + "\n".join(out)) if out else None


def answer(query: str) -> str:
    q = re.sub(r"\s+", " ", str(query or "")).strip()[:300]
    if len(q) < 3:
        return "What should I search for?"
    try:
        out = _online_answer(q)
        if out:
            return out
    except Exception:
        logger.exception("web_search: online answer failed, trying DuckDuckGo")
    try:
        out = _ddg(q)
        if out:
            return out
    except Exception:
        logger.exception("web_search: DuckDuckGo failed")
    return "(couldn't search the web just now)"
