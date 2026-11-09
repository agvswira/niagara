"""Outbound channel adapter. Agents only call deliver(); connecting WhatsApp Cloud API later touches this file only."""


def deliver(channel: str, handle: str, text: str) -> str:
    # The competition build has one channel, the web simulator, which reads messages straight from the database.
    return channel
