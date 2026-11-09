import csv
import io
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import demo, auth, clock, db, events, onboarding, sim
from app.agents.supervisor import build_brief
from app.config import settings
from app.llm.client import get_llm
from app.models import (KV, AgentRun, AgentStep, Approval, BankTransaction, Conversation, Customer,
                        Claim, Event, Invoice, Message, Policy, Product, ProductUom, PromiseToPay, SalesOrder,
                        SalesOrderLine, Stock, User)
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
        fresh = s.query(Customer).count() == 0
        if fresh:
            from app.seed import seed
            seed(s)
        auth.seed_users(s)
    events.drain()
    if fresh:
        demo.run_opening_scenarios()
    yield


app = FastAPI(title="Niagara", docs_url="/api/docs", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

PUBLIC_PATHS = {"/api/auth/login", "/api/auth/me", "/api/docs", "/openapi.json"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every API call and every customer photo needs a signed-in dashboard user."""
    path = request.url.path
    if (path.startswith("/api/") or path.startswith("/media/")) and path not in PUBLIC_PATHS:
        with db.session_scope() as s:
            user = auth.read_token(s, request.cookies.get(auth.COOKIE))
            if user is None:
                return JSONResponse({"detail": "Silakan masuk dulu."}, status_code=401)
            request.state.user = {"username": user.username, "name": user.display_name, "role": user.role}
    response = await call_next(request)
    if path.startswith("/static/"):
        # revalidate with the ETag on every load (a cheap 304), so a browser never pairs new HTML with stale JS or CSS
        response.headers["Cache-Control"] = "no-cache"
    return response


def user_of(request: Request) -> dict:
    return request.state.user


def require_role(request: Request, *roles: str) -> dict:
    user = user_of(request)
    if user["role"] not in roles:
        raise HTTPException(403, f"Hanya {', '.join(auth.ROLES[r] for r in roles)} yang boleh melakukan ini.")
    return user


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: Login, request: Request) -> JSONResponse:
    name = body.username.strip().lower()
    if auth.locked(name):
        raise HTTPException(429, "Terlalu banyak percobaan gagal. Coba lagi 5 menit lagi.")
    with db.session_scope() as s:
        user = s.query(User).filter_by(username=name).first()
        if user is None or not auth.verify_password(body.password, user.password_hash):
            auth.record_failure(name)
            raise HTTPException(401, "Nama pengguna atau kata sandi salah.")
        auth.clear_failures(name)
        token = auth.make_token(s, user.username)
        data = {"username": user.username, "name": user.display_name, "role": user.role}
    resp = JSONResponse(data)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=secure, max_age=auth.SESSION_SECONDS)
    return resp


@app.post("/api/auth/logout")
def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    """Always 200 so the login page can check the session without a console error; user is null when signed out."""
    with db.session_scope() as s:
        user = auth.read_token(s, request.cookies.get(auth.COOKIE))
        if user is None:
            return {"user": None}
        u = {"username": user.username, "name": user.display_name, "role": user.role}
    return {"user": True, **u, "role_label": auth.ROLES[u["role"]],
            "can_decide": sorted(auth.APPROVALS_BY_ROLE.get(u["role"], set())) if u["role"] != "supervisor" else ["*"]}


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
def state(request: Request) -> dict:
    role = user_of(request)["role"]
    with db.session_scope() as s:
        mine = sum(1 for (kind,) in s.query(Approval.kind).filter_by(status="PENDING") if auth.can_decide(role, kind))
        pending_events = s.query(Event).filter(Event.status == "PENDING").count()
        return {
            "now": clock.now(s).isoformat(),
            "company": settings.company_name,
            "llm_mode": get_llm().mode,
            "llm_label": get_llm().label,
            "llm_status": get_llm().status(),
            "pending_events": pending_events,
            "dead_events": s.query(Event).filter(Event.status == "DEAD").count(),
            "approvals_pending": mine,
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
                        "payment_score": round(c.payment_score, 2), "is_new": c.is_new, "verified": c.verified,
                        "credit_balance": c.credit_balance,
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
        runs = (s.query(AgentRun).filter_by(customer_id=cid).order_by(AgentRun.id.desc()).limit(60).all())
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
def approvals(request: Request, status: str = "PENDING") -> list[dict]:
    role = user_of(request)["role"]
    with db.session_scope() as s:
        out = []
        for a in s.query(Approval).filter_by(status=status).order_by(Approval.id.desc()).limit(200):
            if not auth.can_decide(role, a.kind):
                continue
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
            elif a.entity_type == "claim":
                cl = s.get(Claim, a.entity_id)
                d["customer"] = s.get(Customer, cl.customer_id).name
                d["claim"] = {"kind": cl.kind, "amount": cl.amount, "qty_base": cl.qty_base, "source": cl.source,
                              "product": s.get(Product, cl.product_id).name if cl.product_id else None,
                              "order_id": cl.order_id, "order": s.get(SalesOrder, cl.order_id).number, "note": cl.note}
            if a.kind == "pelanggan_baru":
                c = s.get(Customer, a.entity_id)
                d["customer_fields"] = {"name": c.name, "owner_name": c.owner_name, "area": c.area, "tier": c.tier,
                                        "credit_limit": c.credit_limit}
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
    amount: int | None = None
    fields: dict | None = None


@app.post("/api/approvals/{aid}/decide")
def decide(aid: int, body: Decision, request: Request) -> dict:
    user = user_of(request)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "Keputusan harus approve atau reject")
    with db.session_scope() as s:
        a = s.get(Approval, aid)
        if not a or a.status != "PENDING":
            raise HTTPException(409, "Approval sudah diputuskan atau tidak ada")
        if not auth.can_decide(user["role"], a.kind):
            raise HTTPException(403, f"{auth.ROLES[user['role']]} tidak berwenang memutuskan ini.")
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
                audit(s, user["username"], "edit_line", "sales_order_line", ln.id, before=before,
                      after={"product_id": ln.product_id, "qty": ln.qty})
            order.was_edited = True
        if a.kind == "unmatched_payment" and body.customer_id:
            a.payload = {**(a.payload or {}), "customer_id": body.customer_id}
        if a.kind == "klaim_tunai" and body.amount:
            a.payload = {**(a.payload or {}), "confirmed_amount": int(body.amount)}
        if a.kind == "pelanggan_baru" and body.fields:
            allowed = {k: body.fields[k] for k in ("name", "owner_name", "area", "tier", "credit_limit") if body.fields.get(k)}
            if "tier" in allowed and allowed["tier"] not in ("A", "B", "C"):
                raise HTTPException(422, "Tier harus A, B, atau C")
            a.payload = {**(a.payload or {}), "fields": allowed}
        a.status = "APPROVED" if body.decision == "approve" else "REJECTED"
        a.decided_by, a.decided_at = user["username"], clock.now(s)
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
                "transactions": [{"id": t.id, "bank": t.bank, "kind": t.kind, "date": t.tx_date.isoformat(), "amount": t.amount,
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
def set_policy(key: str, body: PolicyValue, request: Request) -> dict:
    user = require_role(request, "supervisor")
    if key not in DEFAULT_POLICIES:
        raise HTTPException(404, "Aturan tidak dikenal")
    default = DEFAULT_POLICIES[key][0]
    if type(body.value) is not type(default) and not (isinstance(default, (int, float)) and isinstance(body.value, (int, float))):
        raise HTTPException(422, f"Tipe nilai harus sama dengan bawaan ({type(default).__name__})")
    with db.session_scope() as s:
        p = s.query(Policy).filter_by(key=key).first()
        before = p.value
        p.value = body.value
        audit(s, user["username"], "set_policy", "policy", p.id, before=before, after=body.value)
    return {"ok": True}


class Autonomy(BaseModel):
    level: int


@app.put("/api/customers/{cid}/autonomy")
def set_autonomy(cid: int, body: Autonomy, request: Request) -> dict:
    user = require_role(request, "supervisor")
    if body.level not in (0, 1, 2):
        raise HTTPException(422, "Level 0, 1, atau 2")
    with db.session_scope() as s:
        c = s.get(Customer, cid)
        audit(s, user["username"], "set_autonomy", "customer", cid, before=c.autonomy_level, after=body.level)
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
def reset(request: Request) -> dict:
    require_role(request, "supervisor")
    from app.seed import reset_and_seed

    with events._lock:
        reset_and_seed()
    for f in settings.media_dir.glob("*"):
        f.unlink(missing_ok=True)
    demo.run_opening_scenarios()
    return {"ok": True}


# ---------- warehouse, delivery reports, manual payments, new contacts ----------

@app.get("/api/stock")
def stock() -> list[dict]:
    with db.session_scope() as s:
        out = []
        for p in s.query(Product).order_by(Product.category, Product.name):
            st = s.query(Stock).filter_by(product_id=p.id).first()
            uoms = {u.uom: u.factor for u in s.query(ProductUom).filter_by(product_id=p.id)}
            big = max(uoms.items(), key=lambda kv: kv[1])
            back = sum(ln.backorder_base for ln in s.query(SalesOrderLine).join(SalesOrder, SalesOrderLine.order_id == SalesOrder.id)
                       .filter(SalesOrderLine.product_id == p.id, SalesOrder.status != "CANCELLED"))
            out.append({"id": p.id, "sku": p.sku, "name": p.name, "category": p.category, "base_uom": p.base_uom,
                        "pack_uom": big[0], "pack_factor": big[1], "on_hand": st.qty_on_hand, "reserved": st.qty_reserved,
                        "available": st.qty_on_hand - st.qty_reserved, "backorder": back})
        return out


class Receive(BaseModel):
    product_id: int
    qty: float
    uom: str | None = None


@app.post("/api/stock/receive")
def receive(body: Receive, request: Request) -> dict:
    user = require_role(request, "admin_order", "supervisor")
    from app.tools.orders import receive_stock

    with db.session_scope() as s:
        p = s.get(Product, body.product_id)
        if p is None:
            raise HTTPException(404, "Produk tidak ditemukan")
        uom = s.query(ProductUom).filter_by(product_id=p.id, uom=body.uom or p.base_uom).first()
        if uom is None:
            raise HTTPException(422, "Satuan tidak dikenal untuk produk ini")
        qty_base = int(round(body.qty * uom.factor))
        res = receive_stock(s, product_id=p.id, qty_base=qty_base, ref=f"GR oleh {user['username']}", actor=user["username"])
        if not res.ok:
            raise HTTPException(422, res.message)
        events.emit(s, "stock.received", "product", p.id, {"qty_base": qty_base})
        data = res.data
    drain_bg()
    return data


class DeliveryIssueIn(BaseModel):
    kind: str
    line_id: int | None = None
    qty: float | None = None
    note: str = ""


@app.post("/api/orders/{oid}/delivery-issue")
def delivery_issue(oid: int, body: DeliveryIssueIn, request: Request) -> dict:
    """The driver's report: short, damaged, or refused at the store."""
    user = require_role(request, "admin_order", "supervisor")
    if body.kind not in ("kurang", "rusak", "ditolak"):
        raise HTTPException(422, "Jenis harus kurang, rusak, atau ditolak")
    from app.agents.common import request_approval
    from app.tools.claims import KIND_LABEL, create_claim

    with db.session_scope() as s:
        qty_base = None
        if body.line_id and body.qty:
            ln = s.get(SalesOrderLine, body.line_id)
            if ln is None or ln.order_id != oid:
                raise HTTPException(422, "Barang bukan bagian dari order ini")
            f = s.query(ProductUom).filter_by(product_id=ln.product_id, uom=ln.uom).first()
            qty_base = int(round(body.qty * (f.factor if f else 1)))
        res = create_claim(s, order_id=oid, kind=body.kind, source="sopir", line_id=body.line_id, qty_base=qty_base,
                           note=body.note or f"laporan {user['username']}")
        if not res.ok:
            raise HTTPException(422, res.message)
        order = s.get(SalesOrder, oid)
        amount_text = f"Rp{res.data['amount']:,}".replace(",", ".")
        request_approval(s, "retur", "claim", res.data["claim_id"],
                         f"Laporan sopir: {KIND_LABEL[body.kind]} pada {order.number}. Usulan nota kredit {amount_text}.",
                         {"order": order.number, "amount": res.data["amount"], "kind": body.kind})
        data = res.data
    return data


class ManualPayment(BaseModel):
    customer_id: int
    kind: str
    amount: int
    collector: str = ""
    giro_due: str | None = None


@app.post("/api/payments/manual")
def manual_payment(body: ManualPayment, request: Request) -> dict:
    user = require_role(request, "staf_ar", "supervisor")
    from app.tools.payments import record_manual_payment

    with db.session_scope() as s:
        if s.get(Customer, body.customer_id) is None:
            raise HTTPException(404, "Pelanggan tidak ditemukan")
        res = record_manual_payment(s, customer_id=body.customer_id, kind=body.kind, amount=body.amount,
                                    actor=user["username"], collector=body.collector, giro_due=body.giro_due)
        if not res.ok:
            raise HTTPException(422, res.message)
        if res.data["status"] == "NEW":
            events.emit(s, "payment.manual_recorded", "bank_transaction", res.data["tx_id"], {})
        data = res.data
    drain_bg()
    return data


@app.post("/api/payments/{tx_id}/bounce")
def bounce(tx_id: int, request: Request) -> dict:
    user = require_role(request, "staf_ar", "supervisor")
    from app.tools.payments import bounce_payment

    with db.session_scope() as s:
        res = bounce_payment(s, tx_id=tx_id, actor=user["username"])
        if not res.ok:
            raise HTTPException(422, res.message)
        events.emit(s, "payment.bounced", "bank_transaction", tx_id, res.data)
        data = res.data
    drain_bg()
    return data


class UnknownMessage(BaseModel):
    handle: str
    text: str


@app.post("/api/simulator/unknown")
def sim_unknown(body: UnknownMessage) -> dict:
    handle = "".join(ch for ch in body.handle if ch.isdigit() or ch == "+")[:20]
    if len(handle) < 8 or not body.text.strip():
        raise HTTPException(422, "Isi nomor (minimal 8 digit) dan pesannya")
    with db.session_scope() as s:
        c = onboarding.inbound_from_unknown(s, "simulator", handle, body.text.strip())
        cid = c.id
    drain_bg()
    return {"customer_id": cid}


@app.get("/")
def landing() -> FileResponse:
    return FileResponse(STATIC / "landing.html", headers={"Cache-Control": "no-cache"})


@app.get("/app")
def index() -> FileResponse:
    # the shell must revalidate, otherwise a browser keeps pointing at old ?v= asset URLs after a release
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/demo")
def demo() -> RedirectResponse:
    # old landing links pointed here; the demo now always goes through the login form
    return RedirectResponse("/app", status_code=303)
