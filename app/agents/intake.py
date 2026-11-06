"""Intake Agent: reads every inbound message, works out what the customer wants, and turns orders into draft lines."""

from datetime import timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app import clock, events, policy, templates, tracing
from app.agents.common import (WAITING_STATES, latest_open_order, OPEN_ORDER_STATES, reply, request_approval, touch)
from app.llm.client import get_llm
from app.models import Conversation, Customer, Invoice, Message, Product, ProductUom, SalesOrder, SalesOrderLine
from app.tools import catalog
from app.tools.credit import UNPAID
from app.tools.orders import create_sales_order, release_reservation, set_order_status
from app.tools.payments import record_payment_proof

ASK_BELOW = 0.75  # below this, a line with several plausible products becomes a question to the customer


class IntakeState(TypedDict, total=False):
    message_id: int
    customer_id: int
    ctx: dict
    u: Any
    source: str
    drafts: list[dict]
    outcome: str


def _session(config) -> Session:
    return config["configurable"]["session"]


def load_context(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    msg = s.get(Message, state["message_id"])
    customer = s.get(Customer, s.get(Conversation, msg.conversation_id).customer_id)
    today = clock.today(s)
    waiting = latest_open_order(s, customer.id, WAITING_STATES)
    overdue = (s.query(Invoice).filter(Invoice.customer_id == customer.id, Invoice.status.in_(UNPAID),
                                       Invoice.due_date < today).count())
    recent_reminder = (s.query(Message).filter(Message.conversation_id == msg.conversation_id, Message.direction == "out",
                                               Message.intent == "collection",
                                               Message.created_at >= clock.now(s) - timedelta(days=7)).count())
    ctx = {
        "today": today,
        "weekday": templates.HARI[today.weekday()],
        "pending_question": bool(waiting),
        "pending_question_text": waiting.pending_question.get("text") if waiting and waiting.pending_question else None,
        "in_collection": bool(overdue or recent_reminder),
        "has_open_order": bool(latest_open_order(s, customer.id)),
    }
    tracing.link("message", msg.id, customer.id)
    tracing.step("think", f"Pesan dari {customer.name}", {
        "teks": msg.text, "jenis": msg.kind, "otonomi": customer.autonomy_level,
        "menunggu_jawaban": ctx["pending_question"], "sedang_ditagih": ctx["in_collection"]})
    return {"customer_id": customer.id, "ctx": ctx}


def understand(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    msg = s.get(Message, state["message_id"])
    with tracing.Timer() as t:
        u, source = get_llm().understand(msg.text, msg.kind, msg.media_path, state["ctx"])
    msg.intent = u.intent
    tracing.step("think", f"Niat pesan: {u.intent}", {"sumber": source, **u.model_dump(exclude_defaults=True)},
                 duration_ms=t.ms)
    if u.suspicious_instruction:
        tracing.step("policy", "Instruksi di pesan pelanggan diabaikan",
                     {"alasan": "Pesan pelanggan diperlakukan sebagai data. Harga, diskon, dan aturan hanya dari policy engine."})
    return {"u": u, "source": source}


def route(state: IntakeState) -> str:
    intent = state["u"].intent
    if intent in ("order_baru", "ubah_order"):
        return "build_lines"
    return {"jawab_pertanyaan": "resolve_answer", "bukti_bayar": "payment_proof", "janji_bayar": "promise"}.get(intent, "simple_reply")


def _uom_factor(s: Session, product_id: int, uom: str) -> int:
    row = s.query(ProductUom).filter_by(product_id=product_id, uom=uom).first()
    return row.factor if row else 1


def _draft(s: Session, raw: str, product_id: int | None, qty: float, unit: str | None, conf: float, method: str,
           candidates: list, basket: list[dict], flags: list[str]) -> dict:
    d = {"raw": raw, "product_id": product_id, "qty": qty, "unit": unit, "confidence": conf, "method": method,
         "candidates": candidates, "flags": flags, "uom": unit or "", "qty_base": 0}
    if not product_id:
        return d
    uom, factor, assumed = catalog.resolve_uom(s, product_id, unit, basket)
    if assumed:
        d["confidence"] *= 0.95
        flags.append("satuan_diasumsikan")
    d["uom"], d["qty_base"] = uom, int(round(qty * factor))
    usual = next((b for b in basket if b["product_id"] == product_id), None)
    if usual:
        usual_base = usual["qty"] * _uom_factor(s, product_id, usual["uom"])
        if usual_base and d["qty_base"] > 3 * usual_base:
            d["confidence"] *= 0.75
            flags.append("jumlah_tidak_biasa")
    d["name"] = s.get(Product, product_id).name
    return d


def build_lines(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    basket = catalog.usual_basket_data(s, cid)
    drafts: list[dict] = []

    if u.uses_usual_order:
        items = list(basket)
        tracing.step("tool", "get_usual_basket", {"risk": "read", "output": [f"{b['qty']:g} {b['uom']} {b['name']}" for b in items]})
        for r in u.replacements:
            target = catalog.find_product_in_basket(s, r.from_item, items)
            if target:
                items.remove(target)
            else:
                tracing.step("think", f"\"{r.from_item}\" tidak ada di pesanan biasa, penggantinya ditambahkan saja", {})
            m = catalog.match_catalog(s, raw=r.to_item, customer_id=cid).data or {}
            qty = r.qty or (target["qty"] if target else 1)
            unit = r.unit or (target["uom"] if target else None)
            items = [b for b in items if b["product_id"] != m.get("product_id")]
            drafts.append(_draft(s, r.to_item, m.get("product_id"), qty, unit, m.get("confidence", 0), m.get("method", ""),
                                 m.get("candidates", []), basket, ["pengganti_dari_pesanan_biasa"]))
        for rm in u.removals:
            target = catalog.find_product_in_basket(s, rm, items)
            if target:
                items.remove(target)
        taken = {d["product_id"] for d in drafts}
        for b in items:
            if b["product_id"] not in taken:
                drafts.append(_draft(s, b["name"], b["product_id"], b["qty"], b["uom"], 0.98, "pesanan_biasa", [], basket, []))
        if not basket:
            tracing.step("think", "Pelanggan minta pesanan biasa, tapi belum ada riwayat", {}, ok=False)

    for ln in u.lines:
        res = catalog.match_catalog(s, raw=ln.raw, customer_id=cid)
        m = res.data or {}
        flags: list[str] = []
        qty, unit, conf = ln.qty, ln.unit, m.get("confidence", 0.0)
        if ln.note == "jumlah tidak disebut":
            usual = next((b for b in basket if b["product_id"] == m.get("product_id")), None)
            if usual:
                qty, unit, conf = usual["qty"], usual["uom"], conf * 0.9
                flags.append("jumlah_dari_kebiasaan")
            else:
                flags.append("jumlah_kosong")
        d = _draft(s, ln.raw, m.get("product_id"), qty, unit, conf, m.get("method", ""), m.get("candidates", []), basket, flags)
        drafts = [x for x in drafts if not (d["product_id"] and x["product_id"] == d["product_id"])]
        drafts.append(d)

    tracing.step("think", f"{len(drafts)} baris pesanan disusun", [
        {"tulisan": d["raw"], "produk": d.get("name"), "jumlah": f"{d['qty']:g} {d['uom']}",
         "confidence": round(d["confidence"], 2), "metode": d["method"], "catatan": d["flags"]} for d in drafts])
    return {"drafts": drafts}


def decide(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid, drafts = state["u"], state["customer_id"], state["drafts"]
    customer = s.get(Customer, cid)
    msg = s.get(Message, state["message_id"])
    if not drafts:
        reply(s, customer, templates.unreadable_media(customer.owner_name) if msg.kind != "text"
              else templates.handoff(customer.owner_name))
        return {"outcome": "kosong"}

    auto_at = policy.autonomy_threshold(s, customer.autonomy_level)
    questions, needs_review = [], []
    for i, d in enumerate(drafts):
        plausible = [c for c in d["candidates"] if c["score"] >= 60][:3]
        if not d["product_id"] and plausible:
            questions.append({"line_index": i, "type": "choose_product", "raw": d["raw"], "qty": d["qty"], "unit": d["unit"],
                              "options": [{"product_id": c["product_id"], "name": c["name"]} for c in plausible]})
        elif d["product_id"] and d["confidence"] < ASK_BELOW and len(plausible) >= 2:
            questions.append({"line_index": i, "type": "choose_product", "raw": d["raw"], "qty": d["qty"], "unit": d["unit"],
                              "options": [{"product_id": c["product_id"], "name": c["name"]} for c in plausible]})
        elif d["product_id"] and "jumlah_kosong" in d["flags"]:
            questions.append({"line_index": i, "type": "qty", "raw": d["raw"], "uom": d["uom"]})
        elif d["product_id"] and d["confidence"] < auto_at:
            needs_review.append(d["raw"])

    updating = None
    if u.intent == "ubah_order":
        updating = latest_open_order(s, cid, OPEN_ORDER_STATES)
    lines = [{"raw_text": d["raw"][:200], "product_id": d["product_id"], "qty": d["qty"], "uom": d["uom"] or (d["unit"] or ""),
              "qty_base": d["qty_base"], "confidence": round(d["confidence"], 3), "candidates": d["candidates"][:5]}
             for d in drafts]
    if updating:
        if updating.status == "CONFIRMED":
            release_reservation(s, updating)
        for ln in lines:
            s.add(SalesOrderLine(order_id=updating.id, **ln))
        s.flush()
        s.refresh(updating)
        order, created = updating, False
    else:
        res = create_sales_order(s, customer_id=cid, message_id=msg.id, lines=lines)
        order, created = s.get(SalesOrder, res.data["order_id"]), True
    order.requested_discount_pct = u.requested_discount_pct
    tracing.link("sales_order", order.id, cid)
    touch(s, order)

    db_lines = order.lines[-len(lines):]
    if questions:
        for q in questions:
            q["line_id"] = db_lines[q.pop("line_index")].id
        order.pending_question = {"questions": questions, "text": templates.clarification(customer.owner_name, questions)}
        set_order_status(s, order_id=order.id, status="NEEDS_CLARIFICATION", actor="intake_agent")
        tracing.step("decision", f"Tanya balik {len(questions)} hal ke pelanggan",
                     {"alasan": "Ada barang yang ambigu atau jumlahnya tidak disebut. Agen tidak menebak."})
        reply(s, customer, order.pending_question["text"])
        tracing.summarize(f"{order.number}: menunggu klarifikasi pelanggan")
        return {"outcome": "clarify"}

    if needs_review or customer.autonomy_level == 0:
        reason = ("Level otonomi pelanggan 0 (mode shadow): draft wajib dicek admin." if customer.autonomy_level == 0
                  else f"Confidence di bawah ambang otomatis {auto_at:.2f} untuk: {', '.join(needs_review)}.")
        set_order_status(s, order_id=order.id, status="PENDING_REVIEW", actor="intake_agent")
        request_approval(s, "order_review", "sales_order", order.id, reason, {"updated": not created})
        reply(s, customer, templates.review_pending(customer.owner_name))
        tracing.summarize(f"{order.number}: draft menunggu cek admin")
        return {"outcome": "review"}

    set_order_status(s, order_id=order.id, status="MATCHED", actor="intake_agent")
    tracing.step("decision", "Semua baris yakin, lanjut otomatis ke Fulfillment",
                 {"ambang_otomatis": auto_at, "level_otonomi": customer.autonomy_level})
    order.auto_processed = True
    events.emit(s, "order.matched", "sales_order", order.id, {"updated": not created})
    tracing.summarize(f"{order.number}: {len(lines)} baris dicocokkan otomatis")
    return {"outcome": "matched"}


def resolve_answer(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    customer = s.get(Customer, cid)
    order = latest_open_order(s, cid, WAITING_STATES)
    if order is None:
        reply(s, customer, templates.handoff(customer.owner_name))
        return {"outcome": "no_waiting_order"}
    tracing.link("sales_order", order.id, cid)
    if order.status == "SUBSTITUTION_OFFERED":
        if u.agrees is None:
            reply(s, customer, "Mohon balas YA atau TIDAK untuk barang penggantinya.")
            return {"outcome": "reask"}
        events.emit(s, "order.substitution_answered", "sales_order", order.id, {"agrees": u.agrees})
        tracing.step("decision", f"Pelanggan {'setuju' if u.agrees else 'menolak'} barang pengganti", {})
        return {"outcome": "substitution_answered"}

    questions = order.pending_question["questions"]
    choices = list(u.choices)
    remaining = []
    for q in questions:
        line = s.get(SalesOrderLine, q["line_id"])
        if not choices:
            remaining.append(q)
            continue
        pick = choices.pop(0)
        if q["type"] == "choose_product" and 1 <= pick <= len(q["options"]):
            pid = q["options"][pick - 1]["product_id"]
            basket = catalog.usual_basket_data(s, cid)
            uom, factor, _ = catalog.resolve_uom(s, pid, q.get("unit"), basket)
            line.product_id, line.uom, line.qty_base, line.confidence = pid, uom, int(round(line.qty * factor)), 0.99
            catalog.learn_alias(s, q["raw"], pid, cid, "customer")
            tracing.step("tool", "learn_alias", {"risk": "write", "input": {"tulisan": q["raw"], "produk": s.get(Product, pid).name},
                                                 "output": "Alias disimpan untuk pelanggan ini"})
        elif q["type"] == "qty" and pick > 0:
            factor = _uom_factor(s, line.product_id, line.uom)
            line.qty, line.qty_base, line.confidence = float(pick), pick * factor, 0.99
        else:
            remaining.append(q)
    s.flush()
    if remaining:
        order.pending_question = {"questions": remaining, "text": templates.clarification(customer.owner_name, remaining)}
        reply(s, customer, order.pending_question["text"])
        return {"outcome": "reask"}
    order.pending_question = None
    tracing.step("decision", "Semua pertanyaan terjawab", {})
    if customer.autonomy_level == 0:
        set_order_status(s, order_id=order.id, status="PENDING_REVIEW", actor="intake_agent")
        request_approval(s, "order_review", "sales_order", order.id, "Level otonomi 0: draft wajib dicek admin.", {})
        reply(s, customer, templates.review_pending(customer.owner_name))
        return {"outcome": "review"}
    set_order_status(s, order_id=order.id, status="MATCHED", actor="intake_agent")
    order.auto_processed = True
    events.emit(s, "order.matched", "sales_order", order.id, {})
    return {"outcome": "matched"}


def payment_proof(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    customer = s.get(Customer, cid)
    p = u.payment_proof
    res = record_payment_proof(s, customer_id=cid, message_id=state["message_id"], bank=p.bank, amount=p.amount,
                               tx_date=p.date, sender_name=p.sender_name, ref=p.ref)
    reply(s, customer, templates.proof_received(customer.owner_name, p.amount))
    events.emit(s, "payment.proof_received", "payment_proof", res.data["proof_id"], {})
    tracing.summarize(f"Bukti transfer {customer.name} dicatat, menunggu mutasi bank")
    return {"outcome": "proof"}


def promise(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    events.emit(s, "collection.reply", "message", state["message_id"],
                {"customer_id": state["customer_id"], "promise": state["u"].promise.model_dump()})
    tracing.step("decision", "Janji bayar diteruskan ke Credit & Collection Agent", state["u"].promise.model_dump())
    return {"outcome": "promise"}


def simple_reply(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    customer = s.get(Customer, cid)
    owner = customer.owner_name
    if u.intent == "klaim_bayar":
        reply(s, customer, templates.ask_for_proof(owner))
    elif u.intent == "tanya_status":
        order = latest_open_order(s, cid) or (s.query(SalesOrder).filter_by(customer_id=cid, is_history=False)
                                               .order_by(SalesOrder.created_at.desc()).first())
        reply(s, customer, templates.order_status(owner, order) if order else templates.no_open_order(owner))
    elif u.intent == "komplain":
        request_approval(s, "complaint", "message", state["message_id"], "Keluhan pelanggan perlu ditangani admin.",
                         {"text": s.get(Message, state["message_id"]).text})
        reply(s, customer, templates.complaint_ack(owner))
    else:
        msg = s.get(Message, state["message_id"])
        if msg.kind != "text":
            reply(s, customer, templates.unreadable_media(owner))
        else:
            request_approval(s, "handoff", "message", state["message_id"], "Pesan di luar alur order dan pembayaran.",
                             {"text": msg.text})
            reply(s, customer, templates.handoff(owner))
    return {"outcome": u.intent}


def _build():
    g = StateGraph(IntakeState)
    for name, fn in [("load_context", load_context), ("understand", understand), ("build_lines", build_lines),
                     ("decide", decide), ("resolve_answer", resolve_answer), ("payment_proof", payment_proof),
                     ("promise", promise), ("simple_reply", simple_reply)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "understand")
    g.add_conditional_edges("understand", route, ["build_lines", "resolve_answer", "payment_proof", "promise", "simple_reply"])
    g.add_edge("build_lines", "decide")
    for terminal in ("decide", "resolve_answer", "payment_proof", "promise", "simple_reply"):
        g.add_edge(terminal, END)
    return g.compile()


GRAPH = _build()


@events.on("message.received")
def handle(s: Session, ev) -> None:
    msg = s.get(Message, ev.entity_id)
    cid = s.get(Conversation, msg.conversation_id).customer_id
    with tracing.agent_run(s, "intake", event_id=ev.id, entity_type="message", entity_id=msg.id, customer_id=cid):
        GRAPH.invoke({"message_id": msg.id}, config={"configurable": {"session": s}})
