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
    tasks: list[str]
    current: str | None


def _session(config) -> Session:
    return config["configurable"]["session"]


def load_context(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    msg = s.get(Message, state["message_id"])
    customer = s.get(Customer, s.get(Conversation, msg.conversation_id).customer_id)
    today = clock.today(s)
    waiting = latest_open_order(s, customer.id, WAITING_STATES)
    ttl = policy.get(s, "conversation.question_ttl_hours")
    if waiting and clock.now(s) - waiting.updated_at > timedelta(hours=ttl):
        tracing.step("think", f"Pertanyaan untuk {waiting.number} sudah lebih dari {ttl} jam, pesan ini tidak dianggap jawaban",
                     {"rule": "conversation.question_ttl_hours"})
        waiting = None
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
        "pii_names": [customer.name, customer.bank_account_name, customer.owner_name.split(" ", 1)[-1]],
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


TASK_NODE = {"bukti_bayar": "payment_proof", "jawab_pertanyaan": "resolve_answer", "ubah_order": "change_order",
             "order_baru": "build_lines", "janji_bayar": "promise", "klaim_bayar": "simple_reply",
             "tanya_status": "simple_reply", "komplain": "complaint", "lainnya": "simple_reply"}
TASK_LABEL = {"bukti_bayar": "catat bukti transfer", "jawab_pertanyaan": "proses jawaban pelanggan",
              "ubah_order": "ubah pesanan yang ada", "order_baru": "susun order baru", "janji_bayar": "catat janji bayar",
              "klaim_bayar": "minta foto bukti transfer", "tanya_status": "jawab status pesanan",
              "komplain": "teruskan keluhan ke admin", "lainnya": "teruskan ke admin"}


def plan(state: IntakeState, config) -> IntakeState:
    """One message can carry several requests; turn them into an ordered task list."""
    u = state["u"]
    tasks: list[str] = []
    for intent in [u.intent, *u.also_intents]:
        if intent not in tasks:
            tasks.append(intent)
    if len(tasks) > 1 and "lainnya" in tasks:
        tasks.remove("lainnya")
    msg = _session(config).get(Message, state["message_id"])
    from app.llm.heuristic import STATUS_RE, _normalize

    if "tanya_status" not in tasks and STATUS_RE.search(_normalize(msg.text or "")):
        # A plain status question is easy to spot; do not let the model drop it from a busy message.
        tasks.append("tanya_status")
    if "bukti_bayar" in tasks and (msg.kind != "image" or u.payment_proof is None):
        # A proof is a photo. "Sudah tf ya" in plain text is only a claim, whatever the model called it.
        tracing.step("policy", "Tidak ada foto, dianggap klaim bayar, bukan bukti", {"jenis_pesan": msg.kind})
        tasks = ["klaim_bayar" if t == "bukti_bayar" else t for t in tasks]
        tasks = list(dict.fromkeys(tasks))
    if "bukti_bayar" in tasks and "klaim_bayar" in tasks:
        tasks.remove("klaim_bayar")
    if len(tasks) > 1:
        tracing.step("think", f"Pesan berisi {len(tasks)} permintaan, dikerjakan berurutan",
                     {"rencana": [f"{i + 1}. {TASK_LABEL[t]}" for i, t in enumerate(tasks)]})
    return {"tasks": tasks}


def next_task(state: IntakeState) -> IntakeState:
    tasks = list(state.get("tasks", []))
    return {"current": tasks[0] if tasks else None, "tasks": tasks[1:]}


def route(state: IntakeState) -> str:
    return TASK_NODE[state["current"]] if state.get("current") else END


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


def resolve_days(s: Session, phrase: str | None, llm_days: int | None) -> int | None:
    """The LLM reads the phrase; the calendar maths is done here. LLMs often miscount weekdays."""
    from app.llm.heuristic import days_from_phrase

    computed = days_from_phrase(phrase or "", clock.today(s))
    if computed is not None and llm_days is not None and computed != llm_days:
        tracing.step("policy", "Hitungan tanggal dari LLM dikoreksi",
                     {"ungkapan": phrase, "versi_llm": llm_days, "versi_kalender": computed})
    return computed if computed is not None else llm_days


def apply_delivery_request(s: Session, order: SalesOrder, u) -> None:
    req = u.requested_delivery
    if req is None:
        return
    days = resolve_days(s, req.date_text, req.days_from_today)
    if days is None:
        return
    order.requested_delivery = clock.today(s) + timedelta(days=max(0, days))
    order.requested_not_before = req.not_before
    tracing.step("think", "Pelanggan minta waktu kirim tertentu",
                 {"ungkapan": req.date_text, "tanggal": order.requested_delivery.isoformat(),
                  "jenis": "paling cepat" if req.not_before else "tepat di hari itu"})


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
    questions, needs_review, unknown = [], [], []
    for i, d in enumerate(drafts):
        plausible = [c for c in d["candidates"] if c["score"] >= 60][:3]
        if not d["product_id"] and not plausible:
            unknown.append(d["raw"])
        elif not d["product_id"] and plausible:
            questions.append({"line_index": i, "type": "choose_product", "raw": d["raw"], "qty": d["qty"], "unit": d["unit"],
                              "options": [{"product_id": c["product_id"], "name": c["name"]} for c in plausible]})
        elif d["product_id"] and d["confidence"] < ASK_BELOW and len(plausible) >= 2:
            questions.append({"line_index": i, "type": "choose_product", "raw": d["raw"], "qty": d["qty"], "unit": d["unit"],
                              "options": [{"product_id": c["product_id"], "name": c["name"]} for c in plausible]})
        elif d["product_id"] and "jumlah_kosong" in d["flags"]:
            questions.append({"line_index": i, "type": "qty", "raw": d["raw"], "uom": d["uom"]})
        elif d["product_id"] and d["confidence"] < auto_at:
            needs_review.append(d["raw"])

    if unknown and len(unknown) == len(drafts):
        request_approval(s, "unknown_item", "message", msg.id,
                         f"Barang tidak ada di katalog: {', '.join(unknown)}. Hubungi pelanggan atau tambahkan ke katalog.",
                         {"items": unknown, "text": msg.text})
        reply(s, customer, templates.unknown_items(customer.owner_name, unknown))
        tracing.summarize(f"Barang di luar katalog dari {customer.name}: {', '.join(unknown)}")
        return {"outcome": "unknown"}

    lines = [{"raw_text": d["raw"][:200], "product_id": d["product_id"], "qty": d["qty"], "uom": d["uom"] or (d["unit"] or ""),
              "qty_base": d["qty_base"], "confidence": round(d["confidence"], 3), "candidates": d["candidates"][:5]}
             for d in drafts]
    res = create_sales_order(s, customer_id=cid, message_id=msg.id, lines=lines)
    order, created = s.get(SalesOrder, res.data["order_id"]), True
    order.requested_discount_pct = u.requested_discount_pct
    apply_delivery_request(s, order, u)
    tracing.link("sales_order", order.id, cid)
    touch(s, order)
    if unknown:
        request_approval(s, "unknown_item", "sales_order", order.id,
                         f"Barang tidak ada di katalog: {', '.join(unknown)}. Order lain tetap diproses.",
                         {"items": unknown})

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
    remaining, not_listed = [], []
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
        elif q["type"] == "choose_product" and pick == len(q["options"]) + 1:
            line.product_id, line.qty_base, line.confidence = None, 0, 0.0
            not_listed.append(q["raw"])
        elif q["type"] == "qty" and pick > 0:
            factor = _uom_factor(s, line.product_id, line.uom)
            line.qty, line.qty_base, line.confidence = float(pick), pick * factor, 0.99
        else:
            remaining.append(q)
    s.flush()
    if not_listed:
        tracing.step("decision", "Pelanggan menyebut barang di luar pilihan katalog", {"barang": not_listed})
        request_approval(s, "unknown_item", "sales_order", order.id,
                         f"Barang tidak ada di katalog: {', '.join(not_listed)}.", {"items": not_listed})
    if remaining:
        order.pending_question = {"questions": remaining, "text": templates.clarification(customer.owner_name, remaining)}
        reply(s, customer, order.pending_question["text"])
        return {"outcome": "reask"}
    order.pending_question = None
    tracing.step("decision", "Semua pertanyaan terjawab", {})
    if not [ln for ln in order.lines if ln.product_id]:
        set_order_status(s, order_id=order.id, status="CANCELLED", actor="intake_agent", note="Tidak ada barang katalog")
        reply(s, customer, templates.unknown_items(customer.owner_name, not_listed))
        return {"outcome": "unknown"}
    if customer.autonomy_level == 0:
        set_order_status(s, order_id=order.id, status="PENDING_REVIEW", actor="intake_agent")
        request_approval(s, "order_review", "sales_order", order.id, "Level otonomi 0: draft wajib dicek admin.", {})
        reply(s, customer, templates.review_pending(customer.owner_name))
        return {"outcome": "review"}
    set_order_status(s, order_id=order.id, status="MATCHED", actor="intake_agent")
    order.auto_processed = True
    events.emit(s, "order.matched", "sales_order", order.id, {})
    return {"outcome": "matched"}


MODIFIABLE = ("RECEIVED", "NEEDS_CLARIFICATION", "PENDING_REVIEW", "MATCHED", "SUBSTITUTION_OFFERED", "STOCK_CHECKED",
              "ON_HOLD_CREDIT", "PENDING_APPROVAL", "CONFIRMED", "BACKORDERED")


def _find_line(s: Session, order: SalesOrder, item: str, cid: int) -> SalesOrderLine | None:
    """Matches 'rinso' in a change request to the order line it refers to."""
    m = catalog.match_catalog(s, raw=item, customer_id=cid).data or {}
    for ln in order.lines:
        if ln.product_id and ln.product_id == m.get("product_id"):
            return ln
    for c in m.get("candidates", []):
        for ln in order.lines:
            if ln.product_id == c["product_id"] and c["score"] >= 80:
                return ln
    for ln in order.lines:
        if catalog.norm(item) and catalog.norm(item) in catalog.norm(ln.raw_text + " " + (ln.product.name if ln.product else "")):
            return ln
    return None


def _set_qty(s: Session, line: SalesOrderLine, qty: float, unit: str | None, cid: int) -> None:
    if unit and unit != line.uom:
        uom, factor, _ = catalog.resolve_uom(s, line.product_id, unit, catalog.usual_basket_data(s, cid))
        line.uom = uom
    else:
        factor = _uom_factor(s, line.product_id, line.uom)
    line.qty, line.qty_base, line.backorder_base = qty, int(round(qty * factor)), 0


def _close_approvals(s: Session, order: SalesOrder) -> None:
    from app.models import Approval

    for a in s.query(Approval).filter_by(entity_type="sales_order", entity_id=order.id, status="PENDING"):
        a.status, a.decided_by = "SUPERSEDED", "intake_agent"


def change_order(state: IntakeState, config) -> IntakeState:
    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    customer = s.get(Customer, cid)
    owner = customer.owner_name
    order = latest_open_order(s, cid, MODIFIABLE)
    if order is None:
        shipped = (s.query(SalesOrder).filter(SalesOrder.customer_id == cid, SalesOrder.is_history.is_(False),
                                              SalesOrder.status.in_(["INVOICED", "DELIVERED"]))
                   .order_by(SalesOrder.created_at.desc()).first())
        if shipped:
            request_approval(s, "handoff", "message", state["message_id"],
                             f"Pelanggan minta ubah {shipped.number} yang sudah dikirim. Mungkin perlu retur.",
                             {"text": s.get(Message, state["message_id"]).text})
            reply(s, customer, templates.already_shipped(owner, shipped))
        else:
            reply(s, customer, templates.no_open_order(owner))
        return {"outcome": "no_order"}
    tracing.link("sales_order", order.id, cid)
    was_confirmed = order.status == "CONFIRMED"

    if any(c.op == "batal_semua" for c in u.changes):
        if was_confirmed:
            release_reservation(s, order)
        _close_approvals(s, order)
        order.pending_question = None
        set_order_status(s, order_id=order.id, status="CANCELLED", actor="intake_agent", note="Dibatalkan pelanggan")
        tracing.step("decision", f"{order.number} dibatalkan atas permintaan pelanggan", {"status_sebelumnya": order.status})
        reply(s, customer, templates.order_cancelled(owner, order))
        tracing.summarize(f"{order.number} dibatalkan pelanggan")
        return {"outcome": "cancelled"}

    if was_confirmed:
        release_reservation(s, order)
    notes, applied = [], []
    for c in u.changes:
        line = _find_line(s, order, c.item or "", cid) if c.item else None
        if c.op == "tambah":
            if line:
                _set_qty(s, line, line.qty + (c.qty or 1), c.unit, cid)
                applied.append(f"{line.product.name} ditambah jadi {templates.qty_text(line.qty, line.uom)}")
            else:
                m = catalog.match_catalog(s, raw=c.item, customer_id=cid).data or {}
                d = _draft(s, c.item, m.get("product_id"), c.qty or 1, c.unit, m.get("confidence", 0), m.get("method", ""),
                           m.get("candidates", []), catalog.usual_basket_data(s, cid), [])
                if d["product_id"] and d["confidence"] >= ASK_BELOW:
                    s.add(SalesOrderLine(order_id=order.id, raw_text=c.item[:200], product_id=d["product_id"], qty=d["qty"],
                                         uom=d["uom"], qty_base=d["qty_base"], confidence=round(d["confidence"], 3)))
                    applied.append(f"{d['name']} {templates.qty_text(d['qty'], d['uom'])} ditambahkan")
                else:
                    notes.append(f"\"{c.item}\" belum bisa kami pastikan, mohon tulis merek dan ukurannya.")
            continue
        if line is None:
            notes.append(f"\"{c.item}\" tidak ada di pesanan {order.number}.")
            continue
        if c.op == "hapus":
            applied.append(f"{line.product.name if line.product else line.raw_text} dihapus")
            s.delete(line)
        elif c.op == "ubah_jumlah" and c.qty is not None:
            if c.qty <= 0:
                applied.append(f"{line.product.name} dihapus")
                s.delete(line)
            else:
                _set_qty(s, line, c.qty, c.unit, cid)
                applied.append(f"{line.product.name} jadi {templates.qty_text(line.qty, line.uom)}")
        elif c.op == "kurangi":
            left = line.qty - (c.qty or 1)
            if left <= 0:
                applied.append(f"{line.product.name} dihapus")
                s.delete(line)
            else:
                _set_qty(s, line, left, None, cid)
                applied.append(f"{line.product.name} dikurangi jadi {templates.qty_text(line.qty, line.uom)}")
    s.flush()
    s.refresh(order)
    apply_delivery_request(s, order, u)
    tracing.step("decision", f"Perubahan pada {order.number}", {"diterapkan": applied, "catatan": notes})

    if not applied:
        reply(s, customer, "\n".join([f"{owner}, pesanan {order.number} belum kami ubah."] + notes))
        if was_confirmed:
            from app.tools.orders import reserve_stock
            reserve_stock(s, order_id=order.id)
        return {"outcome": "nothing_changed"}
    if not [ln for ln in order.lines if ln.product_id]:
        _close_approvals(s, order)
        set_order_status(s, order_id=order.id, status="CANCELLED", actor="intake_agent", note="Semua barang dihapus")
        reply(s, customer, templates.order_cancelled(owner, order))
        return {"outcome": "cancelled"}

    _close_approvals(s, order)
    if order.status == "NEEDS_CLARIFICATION":
        live = {ln.id for ln in order.lines}
        questions = [q for q in (order.pending_question or {}).get("questions", []) if q["line_id"] in live]
        if questions:
            order.pending_question = {"questions": questions, "text": templates.clarification(owner, questions)}
            reply(s, customer, "\n".join([f"Baik {owner}, perubahan kami catat: {'; '.join(applied)}."] + notes)
                  + "\n\n" + order.pending_question["text"])
            return {"outcome": "changed_waiting"}
    order.pending_question = None
    set_order_status(s, order_id=order.id, status="MATCHED", actor="intake_agent")
    events.emit(s, "order.matched", "sales_order", order.id, {"updated": True, "notes": notes})
    tracing.summarize(f"{order.number} diubah pelanggan: {'; '.join(applied)}")
    return {"outcome": "changed"}


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
    current = state.get("current") or u.intent
    if current == "klaim_bayar" and u.cash_paid_to_sales:
        request_approval(s, "klaim_tunai", "message", state["message_id"],
                         f"{customer.name} bilang sudah bayar tunai lewat sales"
                         + (f" sebesar Rp{u.cash_amount:,}".replace(",", ".") if u.cash_amount else "")
                         + ". Cocokkan dengan setoran sales lalu catat.",
                         {"amount": u.cash_amount, "customer_id": cid, "text": s.get(Message, state["message_id"]).text})
        reply(s, customer, templates.cash_claim_ack(owner, u.cash_amount))
        return {"outcome": "cash_claim"}
    if current == "klaim_bayar":
        from app.models import PaymentProof

        recent = (s.query(PaymentProof).filter(PaymentProof.customer_id == cid,
                                               PaymentProof.status == "AWAITING_MUTATION")
                  .order_by(PaymentProof.id.desc()).first())
        if recent:
            tracing.step("think", "Bukti transfer sudah ada, tidak perlu minta lagi", {"nominal": recent.amount})
            reply(s, customer, templates.proof_already_received(owner, recent.amount))
        else:
            reply(s, customer, templates.ask_for_proof(owner))
    elif current == "tanya_status":
        # "pesanan kemarin" never means the order this same message just created.
        q = s.query(SalesOrder).filter(SalesOrder.customer_id == cid, SalesOrder.is_history.is_(False),
                                       (SalesOrder.source_message_id.is_(None))
                                       | (SalesOrder.source_message_id != state["message_id"]))
        order = (q.filter(SalesOrder.status.in_(OPEN_ORDER_STATES)).order_by(SalesOrder.created_at.desc()).first()
                 or q.order_by(SalesOrder.created_at.desc()).first())
        reply(s, customer, templates.order_status(owner, order) if order else templates.no_open_order(owner))
    elif current == "komplain":
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
    return {"outcome": current}


def complaint(state: IntakeState, config) -> IntakeState:
    """Turns 'indomie kurang 1 dus' into a claim on the last delivered order; anything vaguer goes to the admin."""
    from app.tools.claims import KIND_LABEL, create_claim, recent_delivered_order

    s = _session(config)
    u, cid = state["u"], state["customer_id"]
    customer = s.get(Customer, cid)
    owner = customer.owner_name
    text = s.get(Message, state["message_id"]).text
    order = recent_delivered_order(s, cid)
    concrete = [i for i in u.issues if i.kind in ("kurang", "rusak") and i.item]
    missing = [i for i in u.issues if i.kind == "tidak_sampai"]
    if missing:
        target = latest_open_order(s, cid, ("CONFIRMED",)) or order
        if target:
            tracing.link("sales_order", target.id, cid)
            request_approval(s, "complaint", "sales_order", target.id,
                             f"{customer.name} bilang pesanan {target.number} belum sampai. Cek posisi ke sopir.", {"text": text})
            reply(s, customer, templates.not_arrived_ack(owner, target))
            if not concrete:
                return {"outcome": "not_arrived"}
    if not concrete or order is None:
        request_approval(s, "complaint", "message", state["message_id"], "Keluhan pelanggan perlu ditangani admin.",
                         {"text": text})
        reply(s, customer, templates.complaint_ack(owner))
        return {"outcome": "complaint"}

    tracing.link("sales_order", order.id, cid)
    rows, notes = [], []
    for issue in concrete:
        line = _find_line(s, order, issue.item, cid)
        if line is None:
            notes.append(f"\"{issue.item}\" tidak ada di pesanan {order.number}, admin kami cek manual.")
            continue
        qty_base = None
        if issue.qty:
            _, factor, _ = catalog.resolve_uom(s, line.product_id, issue.unit or line.uom, [])
            qty_base = int(round(issue.qty * factor))
        res = create_claim(s, order_id=order.id, kind=issue.kind, source="pelanggan", line_id=line.id,
                           qty_base=qty_base, note=text)
        if not res.ok:
            notes.append(f"{line.product.name}: {res.message}")
            continue
        label = f"{line.product.name}, {KIND_LABEL[issue.kind]}"
        if issue.qty:
            label += f" {issue.qty:g} {issue.unit or line.uom}"
        rows.append(label)
        request_approval(s, "retur", "claim", res.data["claim_id"],
                         f"{customer.name}: {label} pada {order.number}. Usulan nota kredit Rp{res.data['amount']:,}.".replace(",", "."),
                         {"order": order.number, "amount": res.data["amount"], "kind": issue.kind})
    if not rows:
        request_approval(s, "complaint", "sales_order", order.id, "Keluhan pelanggan belum bisa dicocokkan ke barang.",
                         {"text": text, "catatan": notes})
        reply(s, customer, templates.complaint_ack(owner))
        return {"outcome": "complaint"}
    tracing.step("decision", f"{len(rows)} klaim dibuat, menunggu persetujuan admin",
                 {"klaim": rows, "catatan": notes, "alasan": "Uang dan stok baru berubah setelah manusia menyetujui."})
    reply(s, customer, templates.claim_received(owner, order, rows, notes))
    tracing.summarize(f"Klaim {customer.name} atas {order.number}: {'; '.join(rows)}")
    return {"outcome": "claim"}


def _build():
    g = StateGraph(IntakeState)
    handlers = [("decide", decide), ("resolve_answer", resolve_answer), ("payment_proof", payment_proof),
                ("promise", promise), ("simple_reply", simple_reply), ("change_order", change_order),
                ("complaint", complaint)]
    for name, fn in [("load_context", load_context), ("understand", understand), ("plan", plan),
                     ("next_task", next_task), ("build_lines", build_lines), *handlers]:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "understand")
    g.add_edge("understand", "plan")
    g.add_edge("plan", "next_task")
    g.add_conditional_edges("next_task", route, sorted(set(TASK_NODE.values())) + [END])
    g.add_edge("build_lines", "decide")
    for name, _ in handlers:
        g.add_edge(name, "next_task")
    return g.compile()


GRAPH = _build()


@events.on("schedule.stale")
def close_stale(s: Session, ev) -> None:
    """Orders that waited too long for the customer's answer are closed so they do not block stock or the queue."""
    hours = policy.get(s, "conversation.close_unanswered_hours")
    cutoff = clock.now(s) - timedelta(hours=hours)
    stale = (s.query(SalesOrder).filter(SalesOrder.status.in_(WAITING_STATES), SalesOrder.updated_at < cutoff,
                                        SalesOrder.is_history.is_(False)).all())
    if not stale:
        return
    with tracing.agent_run(s, "intake", event_id=ev.id):
        for o in stale:
            customer = s.get(Customer, o.customer_id)
            o.pending_question = None
            _close_approvals(s, o)
            set_order_status(s, order_id=o.id, status="CANCELLED", actor="intake_agent", note="Tidak ada jawaban")
            reply(s, customer, templates.order_closed_unanswered(customer.owner_name, o))
        tracing.step("decision", f"{len(stale)} order ditutup karena tidak dijawab lebih dari {hours} jam",
                     {"order": [o.number for o in stale]})
        tracing.summarize(f"{len(stale)} order tanpa jawaban ditutup")


@events.on("message.received")
def handle(s: Session, ev) -> None:
    msg = s.get(Message, ev.entity_id)
    cid = s.get(Conversation, msg.conversation_id).customer_id
    with tracing.agent_run(s, "intake", event_id=ev.id, entity_type="message", entity_id=msg.id, customer_id=cid):
        GRAPH.invoke({"message_id": msg.id}, config={"configurable": {"session": s}})
