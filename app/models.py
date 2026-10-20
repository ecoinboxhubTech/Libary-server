"""Database models. Names follow SRS section 8.1. Money is stored as whole naira (integers)."""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .timeutil import utcnow

# ---- Enumerations kept as plain strings so SQLite and Postgres behave the same ----
ROLES = ["student", "lecturer", "circulation", "cataloguer", "head_librarian", "finance", "admin"]
STAFF_ROLES = {"circulation", "cataloguer", "head_librarian", "finance", "admin"}
MEMBER_TYPES = ["student", "academic_staff", "non_academic_staff", "visitor"]
MEMBER_STATUSES = ["active", "suspended", "expired"]

COPY_STATUSES = ["available", "on_loan", "reserved", "in_transit", "lost", "damaged", "in_repair", "withdrawn"]
COLLECTIONS = ["general", "reserve", "short_loan", "reference_only", "journals"]
FORMATS = ["print_book", "journal", "report", "past_question", "standard", "digital"]
SUBJECT_AREAS = ["maritime", "aviation", "rail", "road", "logistics", "policy", "other"]


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


INSTITUTION_STATUSES = ["pending", "approved", "rejected", "suspended"]
INSTITUTION_TYPES = ["university", "polytechnic", "college", "secondary_school", "training_centre", "other"]


class Institution(Base):
    """A partner school/college. Staff approve it; its students then borrow like any other member.
    Two API keys: a sandbox key (issued at registration, touches no real data) and a live key (issued on approval).
    Only SHA-256 hashes of keys are stored."""
    __tablename__ = "institutions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # INST0001
    name: Mapped[str] = mapped_column(String(200), index=True)
    type: Mapped[str] = mapped_column(String(30), default="college")
    email: Mapped[str] = mapped_column(String(150), index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(String(300))
    contact_name: Mapped[str | None] = mapped_column(String(150))
    website: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    status_note: Mapped[str | None] = mapped_column(String(300))
    max_students: Mapped[int] = mapped_column(Integer, default=500)
    sandbox_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    live_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    live_key_prefix: Mapped[str | None] = mapped_column(String(20))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Member(Base):
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(primary_key=True)
    id_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)  # matric or staff number
    name: Mapped[str] = mapped_column(String(150))
    member_type: Mapped[str] = mapped_column(String(30), default="student")
    role: Mapped[str] = mapped_column(String(30), default="student")
    programme: Mapped[str | None] = mapped_column(String(120))
    level: Mapped[str | None] = mapped_column(String(30))
    department: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_on: Mapped[datetime | None] = mapped_column(DateTime)
    password_hash: Mapped[str | None] = mapped_column(String(200))  # None = not yet activated
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    reset_code_hash: Mapped[str | None] = mapped_column(String(200))
    reset_expires: Mapped[datetime | None] = mapped_column(DateTime)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institutions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Title(Base):
    __tablename__ = "titles"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    subtitle: Mapped[str | None] = mapped_column(String(300))
    authors: Mapped[str | None] = mapped_column(String(400))  # "A; B; C"
    edition: Mapped[str | None] = mapped_column(String(40))
    publisher: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    isbn: Mapped[str | None] = mapped_column(String(20), index=True)
    subjects: Mapped[str | None] = mapped_column(String(400))  # "A; B; C"
    subject_area: Mapped[str] = mapped_column(String(20), default="other", index=True)
    call_number: Mapped[str | None] = mapped_column(String(60), index=True)
    language: Mapped[str] = mapped_column(String(30), default="English")
    department: Mapped[str | None] = mapped_column(String(120), index=True)
    format: Mapped[str] = mapped_column(String(20), default="print_book", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    retired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Added for the v1 storefront API (/api/v1). slug is the public id the front end uses, e.g. "mar-log".
    slug: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    topics: Mapped[list | None] = mapped_column(JSON)
    is_digital: Mapped[bool] = mapped_column(Boolean, default=False)
    repository_path: Mapped[str | None] = mapped_column(String(300))
    quote: Mapped[str | None] = mapped_column(String(400))
    course: Mapped[str | None] = mapped_column(String(80))
    lecturer: Mapped[str | None] = mapped_column(String(120))
    pages: Mapped[int | None] = mapped_column(Integer)

    copies: Mapped[list["Copy"]] = relationship(back_populates="title")

    def refresh_search_text(self) -> None:
        parts = [self.title, self.subtitle, self.authors, self.subjects, self.isbn, self.call_number,
                 self.subject_area, self.department, self.publisher, self.course, self.lecturer,
                 " ".join(self.topics or [])]
        self.search_text = " ".join(p for p in parts if p).lower()


class Copy(Base):
    __tablename__ = "copies"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    barcode: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    shelf_location: Mapped[str | None] = mapped_column(String(80))
    collection: Mapped[str] = mapped_column(String(20), default="general")
    acquired_on: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    cost: Mapped[int | None] = mapped_column(Integer)  # naira, replacement cost

    title: Mapped[Title] = relationship(back_populates="copies")


class Loan(Base):
    __tablename__ = "loans"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("copies.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    renewals: Mapped[int] = mapped_column(Integer, default=0)
    officer_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    override_reason: Mapped[str | None] = mapped_column(String(300))
    collection_point: Mapped[str | None] = mapped_column(String(30))
    checkout_id: Mapped[str | None] = mapped_column(ForeignKey("checkouts.id"), index=True)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])
    copy: Mapped[Copy] = relationship()

    __table_args__ = (Index("ix_loans_open", "copy_id", "returned_at"),)


class Ebook(Base):
    """Readable e-copy of a title: the page text lives here (one row per title). The uploaded file is not kept."""
    __tablename__ = "ebooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), unique=True, index=True)
    filename: Mapped[str | None] = mapped_column(String(200))
    pages: Mapped[list] = mapped_column(JSON)  # list[str], one entry per page
    page_count: Mapped[int] = mapped_column(Integer)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Hold(Base):
    """Reservation. status: queued | ready | fulfilled | cancelled | expired"""
    __tablename__ = "holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    copy_id: Mapped[int | None] = mapped_column(ForeignKey("copies.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    title: Mapped[Title] = relationship()
    member: Mapped[Member] = relationship()


class LedgerEntry(Base):
    """One ledger for charges, payments, waivers and adjustments (covers SRS Fine + Payment).
    kind: fine | replacement | payment | waiver | adjustment. amount is always positive;
    fine/replacement/adjustment add to what is owed, payment/waiver reduce it."""
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300))
    method: Mapped[str | None] = mapped_column(String(30))  # cash | bank_transfer | pos
    receipt_no: Mapped[str | None] = mapped_column(String(60))
    officer_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])


CHARGE_KINDS = ("fine", "replacement", "adjustment")
CREDIT_KINDS = ("payment", "waiver")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))  # in_app | email
    template: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(15), default="pending")  # pending | sent | failed | logged
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), index=True)
    actor_label: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str | None] = mapped_column(String(40), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(40))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# The audit log is append-only (ADM-2, DR-3). Block edits and deletes at ORM level.
@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_is_immutable(*_):
    raise RuntimeError("Audit log entries cannot be modified or deleted")


class SearchLog(Base):
    __tablename__ = "search_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300))
    result_count: Mapped[int] = mapped_column(Integer)
    member_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SyncReceipt(Base):
    """Idempotency record for offline desk transactions: the same client_id is never applied twice."""
    __tablename__ = "sync_receipts"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


@event.listens_for(Title, "after_insert")
def _assign_slug(mapper, connection, target):
    """Every title gets a stable public slug (the id the /api/v1 front end uses). Runs after the INSERT so the
    uniqueness check sees rows from the same flush."""
    from sqlalchemy.orm.attributes import set_committed_value
    from .slugs import unique_slug
    if target.slug:
        return
    slug = unique_slug(connection, Title.__table__, target.title, target.id)
    connection.execute(Title.__table__.update().where(Title.__table__.c.id == target.id).values(slug=slug))
    set_committed_value(target, "slug", slug)


REQUEST_STATUSES = ["pending", "approved", "rejected", "cancelled"]


class LoanRequest(Base):
    """A member asks to borrow a title; staff approve (which issues the loan) or reject."""
    __tablename__ = "loan_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    status: Mapped[str] = mapped_column(String(15), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    decision_note: Mapped[str | None] = mapped_column(String(300))
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])
    title: Mapped[Title] = relationship()


class ReturnRequest(Base):
    """A member says they are returning a book; staff confirm receipt (which closes the loan) or reject."""
    __tablename__ = "return_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    condition: Mapped[str] = mapped_column(String(10), default="ok")  # ok | damaged
    status: Mapped[str] = mapped_column(String(15), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    decision_note: Mapped[str | None] = mapped_column(String(300))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])
    loan: Mapped[Loan] = relationship()


class PaymentRequest(Base):
    """A member reports a payment (e.g. bank transfer); an admin approves it, which writes the ledger payment."""
    __tablename__ = "payment_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(30))
    receipt_no: Mapped[str] = mapped_column(String(60), index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(15), default="pending", index=True)
    decision_note: Mapped[str | None] = mapped_column(String(300))
    ledger_id: Mapped[int | None] = mapped_column(ForeignKey("ledger.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])


# ---- Storefront checkout and online payments (/api/v1). Money here is integer kobo; the ledger above stays in naira
# and is the single source of truth for what a member owes. A payment clears fines by writing ledger rows. ----
CHECKOUT_STATUSES = ["awaiting_payment", "confirmed", "payment_failed", "expired", "cancelled"]
PAYMENT_STATUSES = ["pending", "success", "failed", "refunded", "abandoned"]
PAYMENT_CHANNELS = ["card", "bank_transfer", "cash"]


class Checkout(Base):
    """A basket the member confirmed. Copies are held (copies.status = reserved) until payment or expiry."""
    __tablename__ = "checkouts"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # chk_ + 8 hex
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    items: Mapped[list] = mapped_column(JSON)  # title slugs/ids as sent
    reserved_copy_ids: Mapped[list] = mapped_column(JSON, default=list)
    collection_point: Mapped[str] = mapped_column(String(30), default="desk")
    period_days: Mapped[int] = mapped_column(Integer, default=14)
    fines_to_pay_kobo: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="awaiting_payment", index=True)
    payment_reference: Mapped[str | None] = mapped_column(String(30), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    member: Mapped[Member] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # NITT_7Q4M2XPD
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), index=True)
    checkout_id: Mapped[str | None] = mapped_column(ForeignKey("checkouts.id"), index=True)
    amount_kobo: Mapped[int] = mapped_column(Integer)
    fee_kobo: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[str] = mapped_column(String(15), default="pending", index=True)
    channel: Mapped[str | None] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(20), default="sandbox")
    purpose: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(150))
    payment_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    gateway_response: Mapped[str | None] = mapped_column(String(200))
    authorization_url: Mapped[str | None] = mapped_column(String(300))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    member: Mapped[Member | None] = relationship()


class PaymentEvent(Base):
    """The per-payment timeline shown in the gateway dashboard."""
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinePayment(Base):
    """Links a payment to the ledger rows it wrote, so a refund can reopen exactly those fines."""
    __tablename__ = "fine_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledger.id"), index=True)
    amount_kobo: Mapped[int] = mapped_column(Integer)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(80))  # provider event id (or sha256 of the body when absent)
    event: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str | None] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    signature: Mapped[str | None] = mapped_column(String(200))  # stored truncated, never in logs
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # failed processing retries

    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)


class SandboxCharge(Base):
    """The sandbox's own record of a charge: what a real provider would hold on its side. A Payment only changes
    when the server verifies this (or receives a signed webhook about it), never from the browser's say-so."""
    __tablename__ = "sandbox_charges"
    reference: Mapped[str] = mapped_column(String(30), primary_key=True)
    status: Mapped[str] = mapped_column(String(15), default="pending")  # pending | success | failed | refunded
    channel: Mapped[str | None] = mapped_column(String(20))
    fee_kobo: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    gateway_response: Mapped[str | None] = mapped_column(String(200))
    card_attempts: Mapped[int] = mapped_column(Integer, default=0)
    otp_attempts: Mapped[int] = mapped_column(Integer, default=0)
    awaiting_otp: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SandboxTransfer(Base):
    __tablename__ = "sandbox_transfers"
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), primary_key=True)
    rrr: Mapped[str] = mapped_column(String(20))
    bank: Mapped[str] = mapped_column(String(60))
    account_number: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class IdempotencyKey(Base):
    """POST /checkouts and POST /payments/* replay the stored response for a repeated Idempotency-Key."""
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)
    route: Mapped[str] = mapped_column(String(80))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
