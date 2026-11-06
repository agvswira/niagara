"""Outbound channel adapters. Agents only call deliver(); swapping WhatsApp Cloud API in later touches this file only."""

import httpx

from app.config import settings
from app.tools.base import ToolError


def deliver(channel: str, handle: str, text: str) -> str:
    if channel == "telegram":
        if not settings.telegram_bot_token:
            return "telegram (token belum diatur, hanya tersimpan)"
        try:
            r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                           json={"chat_id": handle, "text": text}, timeout=10)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError("channel_error", f"Telegram gagal: {exc}", retryable=True) from exc
        return "telegram"
    return channel
