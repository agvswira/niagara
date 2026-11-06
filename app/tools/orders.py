from datetime import date, timedelta

from sqlalchemy.orm import Session

from app import clock, policy
from app.models import AuditLog, Customer, PriceList, Product, SalesOrder, SalesOrderLine, Stock
from app.tools.base import ToolError, tool

# Delivery days per area (0 = Monday). Routes are fixed, so a slot is the next route day after the cut-off.
ROUTES = {
    "Klojen": (0, 2, 4), "Lowokwaru": (0, 2, 4), "Blimbing": (0, 2, 4),
    "Sukun": (1, 3, 5), "Kedungkandang": (1, 3, 5), "Dau": (1, 4), "Singosari": (1, 4),
}
CUTOFF_HOUR = 15


def available(s: Session, product_id: int) -> int:
    st = s.query(Stock).filter_by(product_id=product_id).first()
    return 0 if st is None else st.qty_on_hand - st.qty_reserved


def unit_price(s: Session, product_id: int, tier: str) -> int:
    row = s.query(PriceList).filter_by(product_id=product_id, tier=tier).first()
    if row is None:
        raise ToolError("no_price", f"Harga produk {product_id} untuk tier {tier} belum ada.")
    return row.price_per_base


def audit(s: Session, actor: str, action: str, entity_type: str, entity_id: int, before=None, after=None) -> None:
    s.add(AuditLog(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, before=before,
                   after=after, created_at=clock.now(s)))


@tool("check_stock", "read")
def check_stock(s: Session, needs: dict[int, int]) -> dict:
    out = {}
    for pid, qty_base in needs.items():
        avail = available(s, int(pid))
        out[int(pid)] = {"requested": qty_base, "available": avail, "ok": avail >= qty_base}
    return out


@tool("find_substitutes", "read")
def find_substitutes(s: Session, product_id: int, qty_base: int, tier: str) -> list[dict]:
    p = s.get(Product, product_id)
    base_price = unit_price(s, product_id, tier)
    max_diff = policy.get(s, "substitution.max_price_diff_pct")
    out = []
    for q in s.query(Product).filter(Product.category == p.category, Product.id != p.id):
        if p.variant and q.variant and p.variant != q.variant:
            continue
        price = unit_price(s, q.id, tier)
        diff = (price - base_price) / base_price * 100
        if abs(diff) <= max_diff and available(s, q.id) >= qty_base:
            out.append({"product_id": q.id, "name": q.name, "price_diff_pct": round(diff, 1), "available": available(s, q.id)})
    return sorted(out, key=lambda x: abs(x["price_diff_pct"]))[:3]


@tool("get_delivery_slot", "read")
def get_delivery_slot(s: Session, area: str) -> dict:
    now = clock.now(s)
    days = ROUTES.get(area, (0, 1, 2, 3, 4, 5))
    start = now.date() + timedelta(days=1 if now.hour < CUTOFF_HOUR else 2)
    for i in range(14):
        d = start + timedelta(days=i)
        if d.weekday() in days:
            return {"date": d.isoformat(), "route_days": list(days), "cutoff_hour": CUTOFF_HOUR}
    raise ToolError("no_slot", "Tidak ada jadwal kirim dalam 14 hari.")


def next_order_number(s: Session) -> str:
    n = s.query(SalesOrder).count() + 1
    return f"SO-{clock.now(s):%y%m%d}-{n:04d}"


@tool("create_sales_order", "write")
def create_sales_order(s: Session, customer_id: int, message_id: int, lines: list[dict]) -> dict:
    existing = s.query(SalesOrder).filter_by(source_message_id=message_id).first()
    if existing:
        return {"order_id": existing.id, "number": existing.number, "idempotent_hit": True}
    now = clock.now(s)
    o = SalesOrder(number=next_order_number(s), customer_id=customer_id, status="RECEIVED", source_message_id=message_id,
                   created_at=now, updated_at=now)
    s.add(o)
    s.flush()
    for ln in lines:
        s.add(SalesOrderLine(order_id=o.id, **ln))
    s.flush()
    audit(s, "intake_agent", "create_order", "sales_order", o.id, after={"lines": len(lines)})
    return {"order_id": o.id, "number": o.number, "idempotent_hit": False}


@tool("price_order", "write")
def price_order(s: Session, order_id: int) -> dict:
    """Prices come from the price list only. The LLM never sets a price."""
    o = s.get(SalesOrder, order_id)
    tier = s.get(Customer, o.customer_id).tier
    total = 0
    for ln in o.lines:
        if not ln.product_id:
            continue
        ln.price = unit_price(s, ln.product_id, tier)
        ln.subtotal = ln.price * ln.qty_base
        total += ln.subtotal
    o.total = total
    s.flush()
    return {"order_id": o.id, "total": total}


@tool("set_order_status", "write")
def set_order_status(s: Session, order_id: int, status: str, actor: str, note: str | None = None) -> dict:
    o = s.get(SalesOrder, order_id)
    before = o.status
    o.status = status
    o.updated_at = clock.now(s)
    if note is not None and status in ("ON_HOLD_CREDIT", "PENDING_APPROVAL"):
        o.hold_reason = note
    audit(s, actor, "set_status", "sales_order", order_id, before={"status": before}, after={"status": status, "note": note})
    s.flush()
    return {"order_id": order_id, "from": before, "to": status}


@tool("reserve_stock", "write")
def reserve_stock(s: Session, order_id: int) -> dict:
    o = s.get(SalesOrder, order_id)
    reserved = {}
    for ln in o.lines:
        if not ln.product_id:
            continue
        st = s.query(Stock).filter_by(product_id=ln.product_id).first()
        free = st.qty_on_hand - st.qty_reserved
        take = min(free, ln.qty_base)
        if take < ln.qty_base:
            raise ToolError("stock_changed", f"Stok {ln.product_id} berubah, tersisa {free}.", retryable=False)
        st.qty_reserved += take
        reserved[ln.product_id] = take
    s.flush()
    return reserved


def release_reservation(s: Session, o: SalesOrder) -> None:
    for ln in o.lines:
        if ln.product_id:
            st = s.query(Stock).filter_by(product_id=ln.product_id).first()
            st.qty_reserved = max(0, st.qty_reserved - ln.qty_base)


def ship(s: Session, o: SalesOrder) -> None:
    for ln in o.lines:
        if ln.product_id:
            st = s.query(Stock).filter_by(product_id=ln.product_id).first()
            st.qty_reserved = max(0, st.qty_reserved - ln.qty_base)
            st.qty_on_hand -= ln.qty_base


def due_date_for(customer: Customer, delivered: date) -> date:
    return delivered + timedelta(days=customer.payment_terms_days)
