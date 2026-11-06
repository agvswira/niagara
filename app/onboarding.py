"""Messages from numbers we have never seen: register a provisional customer, then let a supervisor verify it."""

from sqlalchemy.orm import Session

from app import clock, templates
from app.models import Approval, Customer, CustomerContact
from app.tools.messaging import send_message


def inbound_from_unknown(s: Session, channel: str, handle: str, text: str) -> Customer:
    """Called by a channel adapter when the sender is not a known contact. Returns the provisional customer."""
    from app import sim

    contact = s.query(CustomerContact).filter_by(channel=channel, handle=handle).first()
    if contact:
        sim.inbound(s, contact.customer_id, text)
        return s.get(Customer, contact.customer_id)
    tail = handle[-4:] if len(handle) >= 4 else handle
    c = Customer(name=f"Kontak baru {tail}", owner_name="Bapak/Ibu", area="Belum diisi", tier="C",
                 credit_limit=2_000_000, payment_terms_days=7, autonomy_level=0, is_new=True, verified=False,
                 persona="baru", bank_account_name="")
    s.add(c)
    s.flush()
    s.add(CustomerContact(customer_id=c.id, channel=channel, handle=handle))
    s.add(Approval(kind="pelanggan_baru", entity_type="customer", entity_id=c.id,
                   reason=f"Pesan dari nomor yang belum terdaftar ({channel} {handle}). Lengkapi data toko lalu verifikasi.",
                   payload={"channel": channel, "handle": handle, "first_message": text[:500]},
                   created_at=clock.now(s)))
    s.flush()
    send_message(s, customer_id=c.id, text=templates.welcome_unknown(handle), category="order")
    # The message itself still goes through the normal agents; autonomy 0 means a person checks any order first.
    sim.inbound(s, c.id, text)
    return c
