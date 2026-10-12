"""Voice notes and images from the team group -> text the assistant can use.

The bridge downloads a voice note / image posted in an allow-listed group and
sends it here. We turn it into a short text line ("[voice note] ...",
"[image] ...") and store it in wa_group_log like any other message, so promises
made in a voice note are tracked by group_intel and searchable by the agent.

Uses an OpenRouter model that accepts audio and images (default
google/gemini-2.5-flash, override with OPENROUTER_MEDIA_MODEL). Needs
OPENROUTER_API_KEY. Everything returns "" on any failure -- media is a bonus,
never something that can break message handling.
"""
from __future__ import annotations

import base64
import logging
import os

import requests

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def media_model() -> str:
    return os.getenv("OPENROUTER_MEDIA_MODEL", "").strip() or "google/gemini-2.5-flash"


def available() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


def _ask(parts: list, max_tokens: int = 500) -> tuple:
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '').strip()}",
                 "Content-Type": "application/json",
                 "X-Title": "Lumina"},
        json={"model": media_model(), "max_tokens": max_tokens, "temperature": 0.1,
              "messages": [{"role": "user", "content": parts}]},
        timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    u = data.get("usage") or {}
    return str(text).strip(), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _record(tin: int, tout: int, what: str) -> None:
    try:
        from budget_tracker import record_usage
        from model_router import calculate_cost
        record_usage(task_type="whatsapp", model_tier="qwen", model_name=media_model(),
                     input_tokens=tin, output_tokens=tout,
                     cost=calculate_cost("qwen", tin, tout), user_id=f"wa_media_{what}")
    except Exception:
        logger.debug("media_ai: usage record failed", exc_info=True)


def _audio_format(mime: str) -> str:
    m = (mime or "").lower()
    for k, v in (("ogg", "ogg"), ("opus", "ogg"), ("mpeg", "mp3"), ("mp3", "mp3"),
                 ("mp4", "m4a"), ("m4a", "m4a"), ("aac", "aac"), ("wav", "wav"),
                 ("flac", "flac")):
        if k in m:
            return v
    return "ogg"          # WhatsApp voice notes are ogg/opus


def transcribe_audio(data: bytes, mime: str = "audio/ogg") -> str:
    """Verbatim transcript of a voice note (Hindi/English/Hinglish kept as
    spoken, Latin script)."""
    if not available() or not data or len(data) > MAX_AUDIO_BYTES:
        return ""
    try:
        text, tin, tout = _ask([
            {"type": "text", "text": (
                "Transcribe this voice note exactly as spoken. Keep Hindi/English "
                "mixing as is, written in Latin script. Output ONLY the transcript, "
                "no commentary. If it is silent or unintelligible output nothing.")},
            {"type": "input_audio", "input_audio": {
                "data": base64.b64encode(data).decode("ascii"),
                "format": _audio_format(mime)}},
        ], max_tokens=700)
        _record(tin, tout, "audio")
        return text[:1500]
    except Exception:
        logger.exception("media_ai: transcription failed")
        return ""


def describe_image(data: bytes, mime: str = "image/jpeg", caption: str = "") -> str:
    """One or two plain sentences on what a work image shows (client feedback
    screenshot, design, post). Text visible in the image is quoted."""
    if not available() or not data or len(data) > MAX_IMAGE_BYTES:
        return ""
    try:
        text, tin, tout = _ask([
            {"type": "text", "text": (
                "This image was posted in an agency team's work chat"
                + (f" with the caption: {caption!r}" if caption else "")
                + ". In one or two short plain sentences say what it shows. If it has "
                "readable text (client feedback, a deadline, a message), quote the "
                "important words. If it is only a meme, selfie or irrelevant, "
                "output exactly: skip")},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode('ascii')}"}},
        ], max_tokens=200)
        _record(tin, tout, "image")
        return "" if text.strip().lower().startswith("skip") else text[:500]
    except Exception:
        logger.exception("media_ai: image description failed")
        return ""
