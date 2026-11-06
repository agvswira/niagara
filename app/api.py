import csv
import io
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func

from app import clock, db, events, sim
from app.agents.supervisor import build_brief
from app.config import settings
from app.llm.client import get_llm
from app.models import (KV, AgentRun, AgentStep, Approval, BankTransaction, Conversation, Customer, CustomerContact,
                        Event, Invoice, Message, Policy, Product, PromiseToPay, SalesOrder, SalesOrderLine, Stock)
from app.policy import DEFAULT_POLICIES
from app.tools import catalog
from app.tools.credit import UNPAID, snapshot
from app.tools.orders import audit
from app.tools.payments import import_bank_mutations

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    with db.session_scope() as s:
        if s.query(Customer).count() == 0:
            from app.seed import seed
            seed(s)
    events.drain()
    yield


app = FastAPI(title="Niagara", docs_url="/api/docs", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def drain_bg() -> None:
    threading.Thread(target=events.drain, daemon=True).start()


def media_url(path: str | None) -> str | None:
    return f"/media/{Path(path).name}" if path else None


# ---------- read models ----------

def order_dict(o: SalesOrder, lines: bool = False) -> dict:
    d = {"id": o.id, "number": o.number, "customer_id": o.customer_id, "customer": o.customer.name, "status": o.status,
         "total": o.total, "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
         "auto_processed": o.auto_processed, "was_edited": o.was_edited, "hold_reason": o.hold_reason,
         "created_at": o.created_at.isoformat(), "updated_at": o.updated_at.isoformat(),
         "pending_question": (o.pending_question or {}).get("text")}
    if lines:
        d["lines"] = [{"id": ln.id, "raw_text": ln.raw_text, "product_id": ln.product_id,
                       "product": ln.product.name if ln.product else None, "qty": ln.qty, "uom": ln.uom,
                       "qty_base": ln.qty_base, "price": ln.price, "subtotal": ln.subtotal,
                       "confidence": ln.confidence, "backorder_base": ln.backorder_base,
                       "substituted_from": ln.substituted_from, "candidates": ln.candidates or []} for ln in o.lines]
    return d


def run_dict(r: AgentRun, steps: bool = True) -> dict:
    d = {"id": r.id, "agent": r.agent, "status": r.status, "summary": r.summary, "entity_type": r.entity_type,
         "entity_id": r.entity_id, "customer_id": r.customer_id, "llm_calls": r.llm_calls,
         "started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat() if r.ended_at else None}
    if steps:
        d["steps"] = [{"id": st.id, "kind": st.kind, "title": st.title, "detail": st.detail, "ok": st.ok,
                       "duration_ms": st.duration_ms} for st in r.steps]
    return d


@app.get("/api/state")
def state() -> dict:
    with db.session_scope() as s:
        pending_events = s.query(Event).filter(Event.status == "PENDING").count()
        return {
            "now": clock.now(s).isoformat(),
            "company": settings.company_name,
            "llm_mode": get_llm().mode,
            "model": settings.gemini_model if get_llm().mode == "gemini" else None,
            "pending_events": pending_events,
            "dead_events": s.query(Event).filter(Event.status == "DEAD").count(),
            "approvals_pending": s.query(Approval).filter_by(status="PENDING").count(),
            "unmatched_payments": s.query(BankTransaction).filter_by(status="UNMATCHED").count(),
            "bank_feed_waiting": len((s.get(KV, "bank_feed").value if s.get(KV, "bank_feed") else [])),
        }


@app.get("/api/customers")
def customers() -> list[dict]:
    with db.session_scope() as s:
        out = []
        for c in s.query(Customer).order_by(Customer.name):
            snap = snapshot(s, c.id)
            conv = s.query(Conversation).filter_by(customer_id=c.id).first()
            last = (s.query(Message).filter_by(conversation_id=conv.id).order_by(Message.id.desc()).first() if conv else None)
            out.append({"id": c.id, "name": c.name, "owner": c.owner_name, "area": c.area, "tier": c.tier,
                        "autonomy_level": c.autonomy_level, "persona": c.persona, "credit_limit": c.credit_limit,
                        "outstanding": snap["outstanding"], "max_overdue_days": snap["max_overdue_days"],
                        "payment_score": round(c.payment_score, 2), "is_new": c.is_new,
                        "last_message": {"text": last.text[:80], "direction": last.direction,
                                         "at": last.created_at.isoformat()} if last else None})
        return out


@app.get("/api/customers/{cid}/conversation")
def conversation(cid: int) -> dict:
    with db.session_scope() as s:
        c = s.get(Customer, cid)
        if not c:
            raise HTTPException(404, "Pelanggan tidak ditemukan")
        conv = s.query(Conversation).filter_by(customer_id=cid).first()
        msgs = s.query(Message).filter_by(conversation_id=conv.id).order_by(Message.id).all() if conv else []
        order = (s.query(SalesOrder).filter_by(customer_id=cid, is_history=False).order_by(SalesOrder.id.desc()).first())
        runs = (s.query(AgentRun).filter_by(customer_id=cid).order_by(AgentRun.id.desc()).limit(12).all())
        basket = catalog.usual_basket_data(s, cid)
        return {
            "customer": {"id": c.id, "name": c.name, "owner": c.owner_name, "tier": c.tier, "area": c.area,
                         "autonomy_level": c.autonomy_level, "persona": c.persona, "credit_limit": c.credit_limit,
                         **snapshot(s, cid)},
            "messages": [{"id": m.id, "direction": m.direction, "kind": m.kind, "text": m.text, "intent": m.intent,
                          "media_url": media_url(m.media_path), "at": m.created_at.isoformat()} for m in msgs],
            "order": order_dict(order, lines=True) if order else None,
            "runs": [run_dict(r) for r in reversed(runs)],
            "usual_basket": basket,
        }


class SimMessage(BaseModel):
    text: str


class SimPay(BaseModel):
    amount: int | None = None
    send_proof: bool = True
    bank: str = "BCA"


@app.post("/api/simulator/{cid}/message")
def sim_message(cid: int, body: SimMessage) -> dict:
    if not body.text.strip():
        raise HTTPException(422, "Pesan kosong")
    with db.session_scope() as s:
        msg = sim.inbound(s, cid, body.text.strip())
        mid = msg.id
    drain_bg()
    return {"message_id": mid}


@app.post("/api/simulator/{cid}/nota")
def sim_nota(cid: int, body: SimMessage) -> dict:
    if not body.text.strip():
        raise HTTPException(422, "Isi nota kosong")
    path = sim.render_nota(body.text.strip())
    with db.session_scope() as s:
        msg = sim.inbound(s, cid, "", kind="image", media_path=path)
        mid = msg.id
    drain_bg()
    return {"message_id": mid}


@app.post("/api/simulator/{cid}/upload")
async def sim_upload(cid: int, file: UploadFile = File(...), caption: str = Form("")) -> dict:
    ctype = file.content_type or ""
    if not (ctype.startswith("image/") or ctype.startswith("audio/")):
        raise HTTPException(415, "Hanya gambar atau rekaman suara")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "File lebih dari 8 MB")
    ext = Path(file.filename or "").suffix.lower() or (".png" if ctype.startswith("image/") else ".ogg")
    path = settings.media_dir / f"up_{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(data)
    with db.session_scope() as s:
        msg = sim.inbound(s, cid, caption, kind="image" if ctype.startswith("image/") else "audio", media_path=str(path))
        mid = msg.id
    drain_bg()
    return {"message_id": mid}


@app.post("/api/simulator/{cid}/pay")
def sim_pay(cid: int, body: SimPay) -> dict:
    with db.session_scope() as s:
        amount = body.amount or snapshot(s, cid)["overdue_amount"] or snapshot(s, cid)["outstanding"]
        if amount <= 0:
            raise HTTPException(422, "Pelanggan ini tidak punya tagihan terbuka")
        res = sim.customer_pays(s, cid, amount, bank=body.bank, send_proof=body.send_proof)
    drain_bg()
    return {"amount": amount, **res}


# ---------- orders & traces ----------

@app.get("/api/orders")
def orders(status: str | None = None, limit: int = 100) -> list[dict]:
    with db.session_scope() as s:
        q = s.query(SalesOrder).filter(SalesOrder.is_history.is_(False))
        if status:
            q = q.filter(SalesOrder.status.in_(status.split(",")))
        return [order_dict(o) for o in q.order_by(SalesOrder.id.desc()).limit(limit)]


@app.get("/api/orders/{oid}")
def order_detail(oid: int) -> dict:
    with db.session_scope() as s:
        o = s.get(SalesOrder, oid)
        if not o:
            raise HTTPException(404, "Order tidak ditemukan")
        d = order_dict(o, lines=True)
        runs = s.query(AgentRun).filter(AgentRun.entity_type == "sales_order", AgentRun.entity_id == oid).all()
        if o.source_message_id:
            runs = s.query(AgentRun).filter(AgentRun.entity_type == "message", AgentRun.entity_id == o.source_message_id).all() + runs
        d["runs"] = [run_dict(r) for r in sorted(runs, key=lambda r: r.id)]
        return d


@app.get("/api/runs")
def runs(limit: int = 30) -> list[dict]:
    with db.session_scope() as s:
        rows = s.query(AgentRun).order_by(AgentRun.id.desc()).limit(limit).all()
        out = []
        for r in rows:
            d = run_dict(r, steps=False)
            d["customer"] = s.get(Customer, r.customer_id).name if r.customer_id else None
            d["step_count"] = s.query(AgentStep).filter_by(run_id=r.id).count()
            out.append(d)
        return out


@app.get("/api/runs/{rid}")
def run_detail(rid: int) -> dict:
    with db.session_scope() as s:
        r = s.get(AgentRun, rid)
        if not r:
            raise HTTPException(404, "Run tidak ditemukan")
        return run_dict(r)


# ---------- approvals ----------

@app.get("/api/approvals")
def approvals(status: str = "PENDING") -> list[dict]:
    with db.session_scope() as s:
        out = []
        for a in s.query(Approval).filter_by(status=status).order_by(Approval.id.desc()).limit(100):
            d = {"id": a.id, "kind": a.kind, "entity_type": a.entity_type, "entity_id": a.entity_id, "reason": a.reason,
                 "payload": a.payload, "status": a.status, "created_at": a.created_at.isoformat(), "decided_by": a.decided_by}
            if a.entity_type == "sales_order":
                d["order"] = order_dict(s.get(SalesOrder, a.entity_id), lines=True)
            elif a.entity_type == "bank_transaction":
                tx = s.get(BankTransaction, a.entity_id)
                d["transaction"] = {"id": tx.id, "amount": tx.amount, "description": tx.description, "bank": tx.bank,
                                    "date": tx.tx_date.isoformat()}
            elif a.entity_type == "customer":
                d["customer"] = s.get(Customer, a.entity_id).name
            elif a.entity_type == "message":
                m = s.get(Message, a.entity_id)
                d["customer"] = s.get(Customer, s.get(Conversation, m.conversation_id).customer_id).name
            elif a.entity_type == "promise":
                p = s.get(PromiseToPay, a.entity_id)
                d["customer"] = s.get(Customer, p.customer_id).name
            out.append(d)
        return out


class LineEdit(BaseModel):
    line_id: int
    product_id: int | None = None
    qty: float | None = None
    remove: bool = False


class Decision(BaseModel):
    decision: str  # approve / reject
    by: str = "admin"
    edits: list[LineEdit] = []
    customer_id: int | None = None


@app.post("/api/approvals/{aid}/decide")
def decide(aid: int, body: Decision) -> dict:
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "Keputusan harus approve atau reject")
    with db.session_scope() as s:
        a = s.get(Approval, aid)
        if not a or a.status != "PENDING":
            raise HTTPException(409, "Approval sudah diputuskan atau tidak ada")
        if a.kind == "unmatched_payment" and body.decision == "approve" and not body.customer_id:
            raise HTTPException(422, "Pilih pelanggan pemilik transfer ini")
        if body.edits and a.entity_type == "sales_order":
            order = s.get(SalesOrder, a.entity_id)
            basket = catalog.usual_basket_data(s, order.customer_id)
            for e in body.edits:
                ln = s.get(SalesOrderLine, e.line_id)
                if ln is None or ln.order_id != order.id:
                    raise HTTPException(422, f"Baris {e.line_id} bukan milik order ini")
                before = {"product_id": ln.product_id, "qty": ln.qty}
                if e.remove:
                    s.delete(ln)
                    continue
                if e.product_id and e.product_id != ln.product_id:
                    ln.product_id = e.product_id
                    catalog.learn_alias(s, ln.raw_text, e.product_id, order.customer_id, "admin")
                if e.qty is not None:
                    ln.qty = e.qty
                if ln.product_id:
                    uom, factor, _ = catalog.resolve_uom(s, ln.product_id, ln.uom or None, basket)
                    ln.uom, ln.qty_base = uom, int(round(ln.qty * factor))
                ln.confidence = 1.0
                audit(s, body.by, "edit_line", "sales_order_line", ln.id, before=before,
                      after={"product_id": ln.product_id, "qty": ln.qty})
            order.was_edited = True
        if a.kind == "unmatched_payment" and body.customer_id:
            a.payload = {**(a.payload or {}), "customer_id": body.customer_id}
        a.status = "APPROVED" if body.decision == "approve" else "REJECTED"
        a.decided_by, a.decided_at = body.by, clock.now(s)
        events.emit(s, "approval.decided", "approval", a.id, {})
    drain_bg()
    return {"ok": True}


@app.get("/api/products")
def products(q: str = "") -> list[dict]:
    with db.session_scope() as s:
        rows = s.query(Product).order_by(Product.name).all()
        if q:
            ql = q.lower()
            rows = [p for p in rows if ql in p.name.lower() or ql in p.sku.lower()]
        out = []
        for p in rows[:60]:
            st = s.query(Stock).filter_by(product_id=p.id).first()
            out.append({"id": p.id, "sku": p.sku, "name": p.name, "category": p.category, "base_uom": p.base_uom,
                        "available": st.qty_on_hand - st.qty_reserved if st else 0})
        return out


# ---------- receivables & cash ----------

@app.get("/api/ar")
def receivables() -> dict:
    with db.session_scope() as s:
        today = clock.today(s)
        buckets = {"belum_jatuh_tempo": 0, "1_7": 0, "8_30": 0, "lebih_30": 0}
        per_customer = {}
        for inv in s.query(Invoice).filter(Invoice.status.in_(UNPAID)):
            due = inv.amount - inv.paid_amount
            late = (today - inv.due_date).days
            key = "belum_jatuh_tempo" if late <= 0 else "1_7" if late <= 7 else "8_30" if late <= 30 else "lebih_30"
            buckets[key] += due
            c = per_customer.setdefault(inv.customer_id, {"customer_id": inv.customer_id, "customer": inv.customer.name,
                                                          "total": 0, "overdue": 0, "max_late": 0, "invoices": 0,
                                                          "stage": 0, "last_reminder": None})
            c["total"] += due
            c["invoices"] += 1
            if late > 0:
                c["overdue"] += due
                c["max_late"] = max(c["max_late"], late)
            c["stage"] = max(c["stage"], inv.collection_stage)
            if inv.last_reminder_at and (c["last_reminder"] is None or inv.last_reminder_at.isoformat() > c["last_reminder"]):
                c["last_reminder"] = inv.last_reminder_at.isoformat()
        promises = [{"id": p.id, "customer": s.get(Customer, p.customer_id).name, "customer_id": p.customer_id,
                     "date": p.promised_date.isoformat(), "amount": p.amount, "status": p.status}
                    for p in s.query(PromiseToPay).order_by(PromiseToPay.promised_date.desc()).limit(30)]
        return {"today": today.isoformat(), "buckets": buckets,
                "customers": sorted(per_customer.values(), key=lambda c: (-c["max_late"], -c["total"])),
                "promises": promises}


@app.get("/api/bank")
def bank() -> dict:
    with db.session_scope() as s:
        feed = s.get(KV, "bank_feed")
        txs = s.query(BankTransaction).order_by(BankTransaction.id.desc()).limit(100).all()
        return {"feed_waiting": feed.value if feed else [],
                "transactions": [{"id": t.id, "bank": t.bank, "date": t.tx_date.isoformat(), "amount": t.amount,
                                  "description": t.description, "ref": t.ref, "status": t.status,
                                  "customer": s.get(Customer, t.customer_id).name if t.customer_id else None,
                                  "note": t.match_note} for t in txs]}


@app.post("/api/bank/import-feed")
def bank_import_feed() -> dict:
    with db.session_scope() as s:
        res = sim.import_feed(s)
    drain_bg()
    return res


@app.post("/api/bank/upload-csv")
async def bank_upload(file: UploadFile = File(...)) -> dict:
    """CSV columns: tanggal (YYYY-MM-DD), keterangan, nominal, bank, ref."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        try:
            rows.append({"date": r["tanggal"].strip(), "description": r["keterangan"].strip(),
                         "amount": int(str(r["nominal"]).replace(".", "").replace(",", "").strip()),
                         "bank": (r.get("bank") or "BCA").strip(), "ref": (r.get("ref") or f"CSV{i}").strip()})
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, f"Baris {i + 2} tidak valid: {exc}")
    with db.session_scope() as s:
        res = import_bank_mutations(s, rows=rows).data
        if res["created"]:
            events.emit(s, "bank.mutations_imported", payload={"tx_ids": res["created"]})
    drain_bg()
    return res


# ---------- supervisor, policy, simulation ----------

@app.get("/api/brief")
def brief() -> dict:
    with db.session_scope() as s:
        row = s.get(KV, "brief:latest")
        live = build_brief(s)
        return {"latest": row.value if row else None, "live": live}


@app.get("/api/policies")
def policies() -> list[dict]:
    with db.session_scope() as s:
        return [{"key": p.key, "value": p.value, "description": p.description}
                for p in s.query(Policy).order_by(Policy.key)]


class PolicyValue(BaseModel):
    value: object
    by: str = "supervisor"


@app.put("/api/policies/{key}")
def set_policy(key: str, body: PolicyValue) -> dict:
    if key not in DEFAULT_POLICIES:
        raise HTTPException(404, "Aturan tidak dikenal")
    default = DEFAULT_POLICIES[key][0]
    if type(body.value) is not type(default) and not (isinstance(default, (int, float)) and isinstance(body.value, (int, float))):
        raise HTTPException(422, f"Tipe nilai harus sama dengan bawaan ({type(default).__name__})")
    with db.session_scope() as s:
        p = s.query(Policy).filter_by(key=key).first()
        before = p.value
        p.value = body.value
        audit(s, body.by, "set_policy", "policy", p.id, before=before, after=body.value)
    return {"ok": True}


class Autonomy(BaseModel):
    level: int


@app.put("/api/customers/{cid}/autonomy")
def set_autonomy(cid: int, body: Autonomy) -> dict:
    if body.level not in (0, 1, 2):
        raise HTTPException(422, "Level 0, 1, atau 2")
    with db.session_scope() as s:
        c = s.get(Customer, cid)
        audit(s, "supervisor", "set_autonomy", "customer", cid, before=c.autonomy_level, after=body.level)
        c.autonomy_level = body.level
    return {"ok": True}


class Advance(BaseModel):
    days: int = 1


@app.post("/api/sim/advance")
def advance(body: Advance) -> dict:
    if not 1 <= body.days <= 14:
        raise HTTPException(422, "1 sampai 14 hari")
    with db.session_scope() as s:
        res = sim.advance_days(s, body.days)
    events.drain(500)
    return res


@app.post("/api/sim/reset")
def reset() -> dict:
    from app.seed import reset_and_seed

    with events._lock:
        reset_and_seed()
    for f in settings.media_dir.glob("*"):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/channels/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    """Links a Telegram chat to a customer with '/start <id>', then treats its messages like WhatsApp."""
    update = await request.json()
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    if not chat_id:
        return {"ok": True}
    with db.session_scope() as s:
        contact = s.query(CustomerContact).filter_by(channel="telegram", handle=chat_id).first()
        if text.startswith("/start"):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit() and s.get(Customer, int(parts[1])):
                if contact is None:
                    s.add(CustomerContact(customer_id=int(parts[1]), channel="telegram", handle=chat_id))
                return {"ok": True}
        if contact is None:
            return {"ok": True}
        sim.inbound(s, contact.customer_id, text)
    drain_bg()
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
