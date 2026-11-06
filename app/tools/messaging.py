from sqlalchemy.orm import Session

from app import channels, clock, policy
from app.models import Conversation, Customer, CustomerContact, Message
from app.tools.base import ToolError, tool


def conversation_for(s: Session, customer_id: int, channel: str = "simulator") -> Conversation:
    conv = s.query(Conversation).filter_by(customer_id=customer_id).order_by(Conversation.id).first()
    if conv is None:
        conv = Conversation(customer_id=customer_id, channel=channel)
        s.add(conv)
        s.flush()
    return conv


@tool("send_message", "message", retries=1)
def send_message(s: Session, customer_id: int, text: str, category: str = "order") -> dict:
    """category 'collection' obeys quiet hours and the daily cap; order replies always go out."""
    now = clock.now(s)
    conv = conversation_for(s, customer_id)
    if category == "collection":
        if policy.in_quiet_hours(s, now.hour):
            raise ToolError("quiet_hours", "Di luar jam kirim penagihan.")
        cap = policy.get(s, "collection.max_msgs_per_day")
        start = now.replace(hour=0, minute=0, second=0)
        sent_today = (s.query(Message).filter(Message.conversation_id == conv.id, Message.direction == "out",
                                              Message.intent == "collection", Message.created_at >= start).count())
        if sent_today >= cap:
            raise ToolError("daily_cap", f"Sudah {sent_today} pesan penagihan hari ini.")
    msg = Message(conversation_id=conv.id, direction="out", kind="text", text=text, intent=category, created_at=now)
    s.add(msg)
    s.flush()
    contact = (s.query(CustomerContact).filter(CustomerContact.customer_id == customer_id,
                                               CustomerContact.channel != "simulator").first())
    delivered_via = "simulator"
    if contact:
        delivered_via = channels.deliver(contact.channel, contact.handle, text)
    return {"message_id": msg.id, "channel": delivered_via}


def first_name(c: Customer) -> str:
    return c.owner_name
