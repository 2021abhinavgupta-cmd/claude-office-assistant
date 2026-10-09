"""OpenRouter (Qwen) adapter for the WhatsApp agent.

The agent's tool loop (whatsapp_agent.handle_message) is written against
Anthropic's response shape: `resp.content` blocks with `.type` of "text" or
"tool_use", `resp.stop_reason`, `resp.usage.input_tokens/output_tokens`, and
messages that carry tool_use / tool_result blocks. This module speaks
OpenRouter's OpenAI-style chat-completions API but returns that SAME shape, so
the loop does not change and Claude stays a one-flag fallback.

Provider choice, in order:
  1. env / app_settings `whatsapp_llm` = "claude"  -> never use this module
  2. OPENROUTER_API_KEY missing                     -> never use this module
  3. otherwise                                      -> use it (Qwen)

Anthropic server tools (web_search) cannot run on OpenRouter models, so they are
dropped from the tool list on this path -- the bot simply has no web search while
on Qwen. Callers fall back to Claude if a call here raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from types import SimpleNamespace

import requests

from db import get_connection

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.6-27b"
TIMEOUT = 60


class Block(SimpleNamespace):
    """Attribute-style content block (type/text/id/name/input), like the
    Anthropic SDK's. A distinct class so a Claude fallback can tell it apart
    and convert it to a plain dict."""


def model_name() -> str:
    return os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL


def _setting() -> str:
    v = os.getenv("WHATSAPP_LLM", "").strip().lower()
    if v:
        return v
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='whatsapp_llm'").fetchone()
        conn.close()
        return str(row[0]).strip().lower() if row and row[0] else ""
    except Exception:
        return ""


def enabled() -> bool:
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        return False
    return _setting() != "claude"


# -- request translation ------------------------------------------------------

def _tools_to_openai(tools: list) -> list:
    out = []
    for t in tools or []:
        # server tools (web_search_20250305 ...) have no input_schema
        if not isinstance(t, dict) or "input_schema" not in t:
            continue
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        }})
    return out


def _system_text(system) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in (system or [])
                       if isinstance(b, dict) and b.get("type") == "text"
                       and b.get("text"))


def _get(b, k, default=""):
    return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)


# Models OpenRouter documents as honouring explicit `cache_control` (Alibaba-hosted
# Qwen). Anything else would get the marker ignored at best, so it is only sent to
# these. Extend with OPENROUTER_CACHE_MODELS="slug,slug" if OpenRouter adds more.
_CACHE_MODELS = {"qwen/qwen3-max", "qwen/qwen-plus", "qwen/qwen3.6-plus",
                 "qwen/qwen3-coder-plus", "qwen/qwen3-coder-flash"}


def supports_cache_control(model: str | None = None) -> bool:
    m = (model or model_name()).strip().lower()
    extra = {x.strip().lower() for x in os.getenv("OPENROUTER_CACHE_MODELS", "").split(",")
             if x.strip()}
    return m in _CACHE_MODELS or m in extra


def _system_message(system, cache: bool) -> dict:
    """With caching on, keep the system prompt as separate text parts and mark
    only the blocks the caller marked (the big static one) -- the per-message
    memory tail stays uncached so it can't invalidate the cache."""
    if cache and isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                part = {"type": "text", "text": b["text"]}
                if b.get("cache_control"):
                    part["cache_control"] = {"type": "ephemeral"}
                parts.append(part)
        if parts:
            return {"role": "system", "content": parts}
    return {"role": "system", "content": _system_text(system)}


def _messages_to_openai(system, messages: list, cache: bool = False) -> list:
    out = [_system_message(system, cache)]
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(_get(b, "text") for b in content
                           if _get(b, "type") == "text")
            calls = [{
                "id": _get(b, "id"), "type": "function",
                "function": {"name": _get(b, "name"),
                             "arguments": json.dumps(_get(b, "input", {}) or {})},
            } for b in content if _get(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user turn made of tool_result blocks (or text blocks)
            for b in content:
                if _get(b, "type") == "tool_result":
                    out.append({"role": "tool",
                                "tool_call_id": _get(b, "tool_use_id"),
                                "content": str(_get(b, "content", ""))})
                elif _get(b, "type") == "text":
                    out.append({"role": "user", "content": _get(b, "text")})
    return out


# -- response translation -----------------------------------------------------

_THINK = re.compile(r"<think>[\s\S]*?</think>", re.I)


def _parse_response(data: dict):
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    text = _THINK.sub("", msg.get("content") or "").strip()
    blocks = []
    if text:
        blocks.append(Block(type="text", text=text))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        blocks.append(Block(type="tool_use", id=tc.get("id") or f"call_{len(blocks)}",
                            name=fn.get("name", ""), input=args))
    has_calls = any(b.type == "tool_use" for b in blocks)
    u = data.get("usage") or {}
    return SimpleNamespace(
        content=blocks,
        stop_reason="tool_use" if has_calls else "end_turn",
        usage=SimpleNamespace(input_tokens=int(u.get("prompt_tokens") or 0),
                              output_tokens=int(u.get("completion_tokens") or 0)),
    )


def create(*, system, tools, messages: list, max_tokens: int = 600):
    """One chat-completion call. Raises on any failure (callers fall back)."""
    payload = {
        "model": model_name(),
        "messages": _messages_to_openai(system, messages,
                                        cache=supports_cache_control()),
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "reasoning": {"enabled": False},   # no hidden thinking tokens on a chat reply
    }
    oa_tools = _tools_to_openai(tools)
    if oa_tools:
        payload["tools"] = oa_tools
        # only route to hosts that actually support tool calling
        payload["provider"] = {"require_parameters": True}
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '').strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("PUBLIC_BASE_URL", "https://lumina.mmga.agency"),
        "X-Title": "Lumina",
    }
    last = None
    for attempt in range(2):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:300]}")
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"OpenRouter error: {str(data['error'])[:300]}")
            return _parse_response(data)
        except Exception as e:
            last = e
            if attempt == 0 and isinstance(e, (requests.Timeout, requests.ConnectionError)):
                time.sleep(2)
                continue
            raise
    raise last  # pragma: no cover


def to_anthropic_messages(messages: list) -> list:
    """Replace this module's Block objects with plain dicts so the same
    conversation can be handed to the Anthropic SDK (Claude fallback)."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, Block) for b in c):
            nc = []
            for b in c:
                if isinstance(b, Block):
                    if b.type == "text":
                        nc.append({"type": "text", "text": b.text})
                    else:
                        nc.append({"type": "tool_use", "id": b.id,
                                   "name": b.name, "input": b.input})
                else:
                    nc.append(b)
            out.append({**m, "content": nc})
        else:
            out.append(m)
    return out
