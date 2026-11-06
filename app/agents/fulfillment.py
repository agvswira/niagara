"""Fulfillment Agent: checks stock, offers substitutes or partial delivery, and books the next route slot."""

from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app import events, templates, tracing
from app.agents.common import reply, touch
from app.models import Customer, Product, ProductUom, SalesOrder, SalesOrderLine
from app.tools.orders import check_stock, find_substitutes, get_delivery_slot, set_order_status


class FulfillState(TypedDict, total=False):
    order_id: int
    shortages: list[dict]
    offers: list[dict]
    notes: list[str]
    substitution_answer: bool | None


def _s(config) -> Session:
    return config["configurable"]["session"]


def _factor(s: Session, product_id: int, uom: str) -> int:
    row = s.query(ProductUom).filter_by(product_id=product_id, uom=uom).first()
    return row.factor if row else 1


def _qty_text(s: Session, line: SalesOrderLine, base: int) -> str:
    f = _factor(s, line.product_id, line.uom)
    return templates.qty_text(base / f, line.uom) if base % f == 0 else f"{base} {s.get(Product, line.product_id).base_uom}"


def check(state: FulfillState, config) -> FulfillState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    needs: dict[int, int] = {}
    for ln in order.lines:
        if ln.product_id and ln.qty_base > 0:
            needs[ln.product_id] = needs.get(ln.product_id, 0) + ln.qty_base
    res = check_stock(s, needs=needs).data
    shortages = [{"line_id": ln.id, "product_id": ln.product_id, "requested": ln.qty_base,
                  "available": res[ln.product_id]["available"]}
                 for ln in order.lines if ln.product_id and ln.qty_base > 0 and not res[ln.product_id]["ok"]]
    if shortages:
        tracing.step("think", f"Stok kurang untuk {len(shortages)} barang", shortages)
    return {"shortages": shortages, "notes": []}


def route_after_check(state: FulfillState) -> str:
    if state.get("substitution_answer") is not None:
        return "apply_answer"
    return "plan_substitutes" if state["shortages"] else "book_slot"


def plan_substitutes(state: FulfillState, config) -> FulfillState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    offers = []
    if customer.allow_substitution:
        for sh in state["shortages"]:
            subs = find_substitutes(s, product_id=sh["product_id"], qty_base=sh["requested"], tier=customer.tier).data or []
            if subs:
                line = s.get(SalesOrderLine, sh["line_id"])
                best = subs[0]
                offers.append({"line_id": sh["line_id"], "from_product_id": sh["product_id"],
                               "from_name": s.get(Product, sh["product_id"]).name,
                               "available_text": _qty_text(s, line, max(sh["available"], 0)),
                               "to_product_id": best["product_id"], "to_name": best["name"],
                               "price_diff_pct": best["price_diff_pct"]})
    else:
        tracing.step("policy", "Pelanggan tidak mengizinkan barang pengganti", {"rule": "customer.allow_substitution"})
    if offers:
        order.pending_question = {"type": "substitution", "offers": offers,
                                  "text": templates.substitution_offer(customer.owner_name, offers)}
        set_order_status(s, order_id=order.id, status="SUBSTITUTION_OFFERED", actor="fulfillment_agent")
        tracing.step("decision", "Tawarkan barang pengganti ke pelanggan",
                     {"alasan": "Kategori dan varian sama, selisih harga dalam batas policy.",
                      "tawaran": [f"{o['from_name']} -> {o['to_name']}" for o in offers]})
        reply(s, customer, order.pending_question["text"])
        tracing.summarize(f"{order.number}: menunggu jawaban barang pengganti")
    return {"offers": offers}


def route_after_plan(state: FulfillState) -> str:
    return END if state["offers"] else "partial"


def apply_answer(state: FulfillState, config) -> FulfillState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    offers = (order.pending_question or {}).get("offers", [])
    order.pending_question = None
    notes = list(state.get("notes", []))
    if state["substitution_answer"]:
        for o in offers:
            line = s.get(SalesOrderLine, o["line_id"])
            new_uom = s.query(ProductUom).filter_by(product_id=o["to_product_id"], uom=line.uom).first()
            line.substituted_from = line.product_id
            line.product_id = o["to_product_id"]
            if new_uom is None:
                line.uom = s.get(Product, o["to_product_id"]).base_uom
                line.qty = float(line.qty_base)
            notes.append(f"{o['from_name']} diganti {o['to_name']} sesuai persetujuan.")
        tracing.step("decision", "Barang pengganti dipakai", {"jumlah": len(offers)})
        s.flush()
        return {"notes": notes, "shortages": []}
    return {"notes": notes}


def route_after_answer(state: FulfillState) -> str:
    return "partial" if state["shortages"] and not state["substitution_answer"] else "book_slot"


def partial(state: FulfillState, config) -> FulfillState:
    s = _s(config)
    notes = list(state.get("notes", []))
    for sh in state["shortages"]:
        line = s.get(SalesOrderLine, sh["line_id"])
        avail = max(sh["available"], 0)
        line.backorder_base = line.qty_base - avail
        name = s.get(Product, line.product_id).name
        if avail == 0:
            notes.append(f"{name} stoknya kosong, kami kirim begitu stok masuk.")
        else:
            notes.append(f"{name} dikirim {_qty_text(s, line, avail)} dulu, sisanya menyusul.")
        f = _factor(s, line.product_id, line.uom)
        line.qty_base = avail
        line.qty = avail / f
    tracing.step("decision", "Kirim sebagian, sisanya backorder", {"catatan": notes})
    s.flush()
    return {"notes": notes}


def book_slot(state: FulfillState, config) -> FulfillState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    slot = get_delivery_slot(s, area=customer.area).data
    order.delivery_date = date.fromisoformat(slot["date"])
    set_order_status(s, order_id=order.id, status="STOCK_CHECKED", actor="fulfillment_agent")
    touch(s, order)
    events.emit(s, "order.stock_checked", "sales_order", order.id, {"notes": state.get("notes", [])})
    tracing.summarize(f"{order.number}: stok aman, jadwal kirim {slot['date']}")
    return {}


def _build():
    g = StateGraph(FulfillState)
    for n, fn in [("check", check), ("plan_substitutes", plan_substitutes), ("apply_answer", apply_answer),
                  ("partial", partial), ("book_slot", book_slot)]:
        g.add_node(n, fn)
    g.add_edge(START, "check")
    g.add_conditional_edges("check", route_after_check, ["plan_substitutes", "book_slot", "apply_answer"])
    g.add_conditional_edges("plan_substitutes", route_after_plan, ["partial", END])
    g.add_conditional_edges("apply_answer", route_after_answer, ["partial", "book_slot"])
    g.add_edge("partial", "book_slot")
    g.add_edge("book_slot", END)
    return g.compile()


GRAPH = _build()


@events.on("order.matched")
def on_matched(s: Session, ev) -> None:
    order = s.get(SalesOrder, ev.entity_id)
    with tracing.agent_run(s, "fulfillment", event_id=ev.id, entity_type="sales_order", entity_id=order.id,
                           customer_id=order.customer_id):
        GRAPH.invoke({"order_id": order.id}, config={"configurable": {"session": s}})


@events.on("order.substitution_answered")
def on_answer(s: Session, ev) -> None:
    order = s.get(SalesOrder, ev.entity_id)
    with tracing.agent_run(s, "fulfillment", event_id=ev.id, entity_type="sales_order", entity_id=order.id,
                           customer_id=order.customer_id):
        GRAPH.invoke({"order_id": order.id, "substitution_answer": bool(ev.payload.get("agrees"))},
                     config={"configurable": {"session": s}})
