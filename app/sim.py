"""Demo environment: customer messages, rendered images, a fake bank feed, and the time machine.
This module stands in for WhatsApp, the bank and the warehouse; the agents do not know it exists."""

import json
import random
import uuid
from datetime import timedelta

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo
from sqlalchemy.orm import Session

from app import clock, events
from app.config import settings
from app.models import KV, Customer, Invoice, Message, SalesOrder
from app.tools.messaging import conversation_for
from app.tools.orders import due_date_for, ship
from app.tools.payments import import_bank_mutations


def inbound(s: Session, customer_id: int, text: str = "", kind: str = "text", media_path: str | None = None,
            meta: dict | None = None) -> Message:
    conv = conversation_for(s, customer_id)
    msg = Message(conversation_id=conv.id, direction="in", kind=kind, text=text, media_path=media_path, meta=meta,
                  created_at=clock.now(s))
    s.add(msg)
    s.flush()
    events.emit(s, "message.received", "message", msg.id)
    return msg


def _font(size: int):
    for name in ("segoepr.ttf", "comic.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _save(img: Image.Image, meta: dict) -> str:
    info = PngInfo()
    info.add_text("niagara", json.dumps(meta))
    path = settings.media_dir / f"{uuid.uuid4().hex[:12]}.png"
    img.save(path, pnginfo=info)
    return str(path)


def render_nota(text: str) -> str:
    """Handwritten-looking order note. The PNG carries the text so offline mode can read it without OCR."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    img = Image.new("RGB", (520, 120 + 46 * len(lines)), (250, 246, 232))
    d = ImageDraw.Draw(img)
    rng = random.Random(text)
    for y in range(70, img.height, 46):
        d.line([(20, y + 34), (500, y + 34)], fill=(200, 210, 230), width=1)
    d.text((24, 20), "NOTA PESANAN", font=_font(26), fill=(120, 40, 40))
    for i, ln in enumerate(lines):
        d.text((30 + rng.randint(-4, 6), 76 + i * 46 + rng.randint(-3, 3)), ln, font=_font(28), fill=(30, 40, 110))
    return _save(img, {"type": "nota", "text": text})


def render_transfer(bank: str, amount: int, date_iso: str, sender: str, ref: str, to_name: str) -> str:
    img = Image.new("RGB", (420, 560), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 420, 90], fill=(0, 82, 155))
    d.text((24, 28), f"{bank} mobile", font=_font(30), fill=(255, 255, 255))
    d.text((24, 120), "Transfer Berhasil", font=_font(28), fill=(20, 120, 60))
    rows = [("Nominal", "Rp" + f"{amount:,}".replace(",", ".")), ("Tanggal", date_iso), ("Dari", sender),
            ("Ke", to_name), ("No. Ref", ref)]
    for i, (k, v) in enumerate(rows):
        d.text((24, 190 + i * 64), k, font=_font(18), fill=(110, 110, 110))
        d.text((24, 214 + i * 64), v, font=_font(24), fill=(20, 20, 20))
    return _save(img, {"type": "transfer", "bank": bank, "amount": amount, "date": date_iso, "sender_name": sender, "ref": ref})


def _feed(s: Session) -> list[dict]:
    row = s.get(KV, "bank_feed")
    return list(row.value) if row else []


def _set_feed(s: Session, items: list[dict]) -> None:
    row = s.get(KV, "bank_feed")
    if row is None:
        s.add(KV(key="bank_feed", value=items))
    else:
        row.value = items
    s.flush()


def customer_pays(s: Session, customer_id: int, amount: int, bank: str = "BCA", send_proof: bool = True,
                  description: str | None = None) -> dict:
    """The customer transfers money: a mutation lands in the bank feed and, optionally, a proof photo is sent."""
    c = s.get(Customer, customer_id)
    today = clock.today(s).isoformat()
    ref = f"{bank[:3]}{uuid.uuid4().hex[:10].upper()}"
    _set_feed(s, _feed(s) + [{"bank": bank, "date": today, "amount": amount, "ref": ref,
                              "description": description or f"TRSF E-BANKING CR {c.bank_account_name}"}])
    msg = None
    if send_proof:
        path = render_transfer(bank, amount, today, c.bank_account_name, ref, settings.company_name)
        msg = inbound(s, customer_id, text="sudah transfer ya", kind="image", media_path=path)
    return {"ref": ref, "message_id": msg.id if msg else None}


def import_feed(s: Session) -> dict:
    items = _feed(s)
    if not items:
        return {"created": [], "skipped_duplicates": 0}
    res = import_bank_mutations(s, rows=items).data
    _set_feed(s, [])
    if res["created"]:
        events.emit(s, "bank.mutations_imported", payload={"tx_ids": res["created"]})
    return res


def deliver_due_orders(s: Session) -> list[str]:
    today = clock.today(s)
    done = []
    for o in s.query(SalesOrder).filter(SalesOrder.status == "CONFIRMED", SalesOrder.delivery_date <= today):
        ship(s, o)
        o.status = "INVOICED"
        customer = s.get(Customer, o.customer_id)
        n = s.query(Invoice).count() + 1
        s.add(Invoice(number=f"INV-{today:%y%m%d}-{n:04d}", order_id=o.id, customer_id=o.customer_id, amount=o.total,
                      issue_date=today, due_date=due_date_for(customer, today), status="OPEN"))
        done.append(o.number)
    s.flush()
    return done


def advance_days(s: Session, days: int = 1) -> dict:
    log = []
    for _ in range(days):
        new = clock.now(s).replace(hour=8, minute=0, second=0) + timedelta(days=1)
        clock.set_now(s, new)
        delivered = deliver_due_orders(s)
        events.emit(s, "schedule.daily", payload={"date": new.date().isoformat()})
        log.append({"date": new.date().isoformat(), "delivered": delivered})
    return {"now": clock.now(s).isoformat(), "days": log}
