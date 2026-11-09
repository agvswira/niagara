"""Outbound channel adapters. Agents only call deliver(); swapping WhatsApp Cloud API in later touches this file only."""

import html
import re

import httpx

from app.config import settings
from app.tools.base import ToolError


def deliver(channel: str, handle: str, text: str) -> str:
    if channel == "telegram":
        if not settings.telegram_bot_token:
            return "telegram (token belum diatur, hanya tersimpan)"
        try:
            # templates mark key figures WhatsApp-style (*Rp1.000*); Telegram needs that as HTML bold
            body = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", html.escape(text, quote=False))
            r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                           json={"chat_id": handle, "text": body, "parse_mode": "HTML"}, timeout=10)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError("channel_error", f"Telegram gagal: {exc}", retryable=True) from exc
        return "telegram"
    return channel
