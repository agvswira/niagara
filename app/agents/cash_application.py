"""Cash Application Agent: matches bank mutations to proofs and invoices, then releases held orders."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app import events, templates, tracing
from app.agents.common import paid_invoice_rows, reply, request_approval
from app.agents.credit import release_held_orders
from app.models import BankTransaction, Customer, PaymentProof, SalesOrder
from app.tools import credit as credit_tools
from app.tools.base import rupiah
from app.tools.payments import add_to_balance, allocate_payment, identify_payer, plan_allocation, unmatched


class CashState(TypedDict, total=False):
    tx_id: int
    payer: dict
    plan: dict


def _s(config) -> Session:
    return config["configurable"]["session"]


def identify(state: CashState, config) -> CashState:
    s = _s(config)
    payer = identify_payer(s, tx_id=state["tx_id"]).data
    return {"payer": payer}


def route_identify(state: CashState) -> str:
    return "plan" if state["payer"]["customer_id"] else "park_unknown"


def plan(state: CashState, config) -> CashState:
    s = _s(config)
    tx = s.get(BankTransaction, state["tx_id"])
    p = plan_allocation(s, customer_id=state["payer"]["customer_id"], amount=tx.amount).data
    tracing.step("think", f"Rencana alokasi: {p['method']}", p)
    return {"plan": p}


def route_plan(state: CashState) -> str:
    p = state["plan"]
    trusted = state["payer"]["method"] in ("bukti_transfer", "tunai_sales", "giro")
    if not p["allocations"]:
        # A payer we are sure of with nothing open to pay: keep it as saldo titipan.
        return "post" if trusted else "park_unknown"
    # A name-only match with leftover money is too uncertain to post automatically.
    if state["payer"]["method"] == "nama_rekening" and p["unapplied"] > 0:
        return "park_unknown"
    return "post"


def post(state: CashState, config) -> CashState:
    s = _s(config)
    tx = s.get(BankTransaction, state["tx_id"])
    cid = state["payer"]["customer_id"]
    p = state["plan"]
    res = allocate_payment(s, tx_id=tx.id, customer_id=cid, allocations=p["allocations"], fee_writeoff=p.get("fee_writeoff", 0))
    if not res.ok:
        return {}
    if state["payer"].get("proof_id"):
        proof = s.get(PaymentProof, state["payer"]["proof_id"])
        proof.status, proof.bank_tx_id = "MATCHED", tx.id
    if p["unapplied"] > 0:
        add_to_balance(s, customer_id=cid, amount=p["unapplied"], reason=f"kelebihan bayar {tx.ref}")
        tracing.step("decision", f"Kelebihan {rupiah(p['unapplied'])} disimpan sebagai saldo titipan",
                     {"alasan": "Tidak ada tagihan terbuka yang tersisa. Saldo otomatis dipakai untuk invoice berikutnya."})
    released = release_held_orders(s, cid)
    customer = s.get(Customer, cid)
    snap = credit_tools.snapshot(s, cid)
    text = templates.payment_applied(customer.owner_name, tx.amount, paid_invoice_rows(s, res.data["invoices"]), released,
                                     balance_added=p["unapplied"], outstanding_after=snap["outstanding"])
    still_held = s.query(SalesOrder).filter_by(customer_id=cid, status="ON_HOLD_CREDIT").all()
    if still_held:
        minimum = credit_tools.release_amount(s, customer, still_held[0].total, snap)
        text += "\n\n" + templates.still_held(still_held, minimum)
        tracing.step("decision", "Order masih ditahan setelah pembayaran", {"order": [o.number for o in still_held],
                                                                             "sisa_piutang": snap["outstanding"]})
    reply(s, customer, text)
    events.emit(s, "payment.applied", "bank_transaction", tx.id, {"customer_id": cid, "released": [o.id for o in released]})
    tracing.summarize(f"{rupiah(tx.amount)} dari {customer.name} dialokasikan ke {', '.join(res.data['invoices']) or 'saldo titipan'}"
                      + (f", {len(released)} order dilepas" if released else ""))
    return {}


def park_unknown(state: CashState, config) -> CashState:
    s = _s(config)
    tx = s.get(BankTransaction, state["tx_id"])
    note = ("Pengirim tidak dikenali dari bukti transfer maupun nama rekening." if not state["payer"]["customer_id"]
            else "Nominal tidak cocok dengan tagihan terbuka, perlu dicek staf AR.")
    unmatched(s, tx, note, [])
    request_approval(s, "unmatched_payment", "bank_transaction", tx.id, note,
                     {"amount": tx.amount, "description": tx.description, "payer": state["payer"]})
    tracing.summarize(f"Mutasi {rupiah(tx.amount)} ({tx.description}) masuk antrian AR")
    return {}


def _build():
    g = StateGraph(CashState)
    for n, fn in [("identify", identify), ("plan", plan), ("post", post), ("park_unknown", park_unknown)]:
        g.add_node(n, fn)
    g.add_edge(START, "identify")
    g.add_conditional_edges("identify", route_identify, ["plan", "park_unknown"])
    g.add_conditional_edges("plan", route_plan, ["post", "park_unknown"])
    g.add_edge("post", END)
    g.add_edge("park_unknown", END)
    return g.compile()


GRAPH = _build()


def process_transaction(s: Session, tx_id: int, event_id: int | None) -> None:
    with tracing.agent_run(s, "cash_application", event_id=event_id, entity_type="bank_transaction", entity_id=tx_id):
        GRAPH.invoke({"tx_id": tx_id}, config={"configurable": {"session": s}})


@events.on("bank.mutations_imported")
def on_import(s: Session, ev) -> None:
    for tx_id in ev.payload.get("tx_ids", []):
        if s.get(BankTransaction, tx_id).status == "NEW":
            process_transaction(s, tx_id, ev.id)


@events.on("payment.manual_recorded")
def on_manual(s: Session, ev) -> None:
    tx = s.get(BankTransaction, ev.entity_id)
    if tx.status == "NEW":
        process_transaction(s, tx.id, ev.id)


@events.on("payment.bounced")
def on_bounced(s: Session, ev) -> None:
    data = ev.payload
    customer = s.get(Customer, data["customer_id"])
    with tracing.agent_run(s, "cash_application", event_id=ev.id, entity_type="bank_transaction", entity_id=ev.entity_id,
                           customer_id=customer.id):
        tracing.step("decision", "Giro ditolak bank, alokasi dibatalkan",
                     {"invoice_dibuka_lagi": data["reopened"], "skor_bayar_baru": round(customer.payment_score, 2)})
        reply(s, customer, templates.giro_bounced(customer.owner_name, data["amount"], data["reopened"]))
        tracing.summarize(f"Giro {rupiah(data['amount'])} dari {customer.name} ditolak bank")


@events.on("schedule.giro")
def on_giro_day(s: Session, ev) -> None:
    """Giros whose due date has come are treated as cleared and applied like any other payment."""
    from app import clock

    due = s.query(BankTransaction).filter(BankTransaction.status == "GIRO_PENDING",
                                          BankTransaction.tx_date <= clock.today(s)).all()
    for tx in due:
        tx.status = "NEW"
        process_transaction(s, tx.id, ev.id)


@events.on("payment.proof_received")
def on_proof(s: Session, ev) -> None:
    """A proof can arrive after its mutation. Retry any mutation that is still waiting for a payer."""
    waiting = s.query(BankTransaction).filter(BankTransaction.status.in_(["NEW", "UNMATCHED"])).all()
    proof = s.get(PaymentProof, ev.entity_id)
    for tx in waiting:
        if tx.amount == proof.amount:
            tx.status = "NEW"
            process_transaction(s, tx.id, ev.id)
