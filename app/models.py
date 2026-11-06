from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_name: Mapped[str] = mapped_column(String(80))
    area: Mapped[str] = mapped_column(String(60))
    tier: Mapped[str] = mapped_column(String(1))  # A/B/C, drives price list and max discount
    credit_limit: Mapped[int] = mapped_column(Integer)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)
    allow_substitution: Mapped[bool] = mapped_column(Boolean, default=True)
    autonomy_level: Mapped[int] = mapped_column(Integer, default=0)
    payment_score: Mapped[float] = mapped_column(Float, default=1.0)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    persona: Mapped[str] = mapped_column(String(40), default="rapi")
    bank_account_name: Mapped[str] = mapped_column(String(120), default="")
    contacts: Mapped[list["CustomerContact"]] = relationship(back_populates="customer")


class CustomerContact(Base):
    __tablename__ = "customer_contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[str] = mapped_column(String(20))  # simulator / telegram / whatsapp
    handle: Mapped[str] = mapped_column(String(80))
    customer: Mapped[Customer] = relationship(back_populates="contacts")
    __table_args__ = (UniqueConstraint("channel", "handle"),)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str] = mapped_column(String(60))
    category: Mapped[str] = mapped_column(String(40))
    variant: Mapped[str] = mapped_column(String(40), default="")
    base_uom: Mapped[str] = mapped_column(String(10), default="pcs")


class ProductUom(Base):
    __tablename__ = "product_uom"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    uom: Mapped[str] = mapped_column(String(10))
    factor: Mapped[int] = mapped_column(Integer)  # how many base units in one of this uom


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    alias: Mapped[str] = mapped_column(String(120), index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    confirmations: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="seed")  # seed / admin / customer


class Stock(Base):
    __tablename__ = "stock"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), unique=True)
    qty_on_hand: Mapped[int] = mapped_column(Integer)  # in base uom
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0)


class PriceList(Base):
    __tablename__ = "price_lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    tier: Mapped[str] = mapped_column(String(1))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    price_per_base: Mapped[int] = mapped_column(Integer)


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(30), index=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    delivery_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    pending_question: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    hold_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_delivery: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    requested_not_before: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_discount_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    was_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_processed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_history: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    lines: Mapped[list["SalesOrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan",
                                                         order_by="SalesOrderLine.id")
    customer: Mapped[Customer] = relationship()


class SalesOrderLine(Base):
    __tablename__ = "sales_order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"))
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    raw_text: Mapped[str] = mapped_column(String(200))
    qty: Mapped[float] = mapped_column(Float)
    uom: Mapped[str] = mapped_column(String(10))
    qty_base: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[int] = mapped_column(Integer, default=0)
    subtotal: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    candidates: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    substituted_from: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    backorder_base: Mapped[int] = mapped_column(Integer, default=0)
    order: Mapped[SalesOrder] = relationship(back_populates="lines")
    product: Mapped[Optional[Product]] = relationship(foreign_keys=[product_id])


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(Text, default="")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(String(3))  # in / out
    kind: Mapped[str] = mapped_column(String(10), default="text")  # text / image / audio
    text: Mapped[str] = mapped_column(Text, default="")
    media_path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales_orders.id"), nullable=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    amount: Mapped[int] = mapped_column(Integer)
    paid_amount: Mapped[int] = mapped_column(Integer, default=0)
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    collection_stage: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    customer: Mapped[Customer] = relationship()


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    bank: Mapped[str] = mapped_column(String(20))
    tx_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    ref: Mapped[str] = mapped_column(String(60), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="NEW")  # NEW / MATCHED / UNMATCHED
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    match_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    candidates: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)


class PaymentProof(Base):
    __tablename__ = "payment_proofs"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    bank: Mapped[str] = mapped_column(String(20), default="")
    amount: Mapped[int] = mapped_column(Integer, default=0)
    tx_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sender_name: Mapped[str] = mapped_column(String(120), default="")
    ref: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default="AWAITING_MUTATION")
    bank_tx_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_transactions.id"), nullable=True)


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    bank_tx_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"))
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    amount: Mapped[int] = mapped_column(Integer)
    fee_writeoff: Mapped[int] = mapped_column(Integer, default=0)


class PromiseToPay(Base):
    __tablename__ = "promises_to_pay"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    promised_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN / KEPT / BROKEN
    source_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # order_review / credit / discount / unmatched_payment
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    decided_by: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    value: Mapped[Any] = mapped_column(JSON)
    description: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.id"), nullable=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    summary: Mapped[str] = mapped_column(Text, default="")
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    steps: Mapped[list["AgentStep"]] = relationship(back_populates="run", order_by="AgentStep.id")


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(20))  # think / tool / policy / decision / message / error
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    run: Mapped[AgentRun] = relationship(back_populates="steps")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer)
    before: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(20))  # admin_order / staf_ar / supervisor
    password_hash: Mapped[str] = mapped_column(String(200))


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
