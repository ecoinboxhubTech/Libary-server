"""Checkout: hold copies, optionally take payment for fines, then issue the loans (brief section 6.2).

A checkout never issues loans until it is `confirmed`. The only things that confirm one are this module's own
no-payment path and a payment that the provider has verified as successful."""
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..apierrors import ApiError
from ..database import SessionLocal
from ..models import Checkout, Copy, Hold, Loan, Member, Payment, Title
from ..timeutil import utcnow
from . import circulation as circ
from . import fines, payments, settings_store
from .audit import audit

OPEN_STATUSES = ("awaiting_payment", "payment_failed")


def new_checkout_id(db: Session) -> str:
    while True:
        cid = "chk_" + secrets.token_hex(4)
        if not db.get(Checkout, cid):
            return cid


def open_checkout(db: Session, member_id: int) -> Checkout | None:
    return db.scalar(select(Checkout).where(Checkout.member_id == member_id, Checkout.status.in_(OPEN_STATUSES)))


def _pick_copy(db: Session, title: Title, member: Member) -> tuple[Copy, bool] | None:
    """The member's own ready hold wins; otherwise the first free, borrowable copy."""
    hold = db.scalar(select(Hold).where(Hold.title_id == title.id, Hold.member_id == member.id,
                                        Hold.status == "ready", Hold.copy_id.isnot(None)))
    if hold:
        return db.get(Copy, hold.copy_id), True
    copy = db.scalar(select(Copy).where(Copy.title_id == title.id, Copy.status == "available",
                                        Copy.collection != "reference_only").order_by(Copy.id)
                     .with_for_update(skip_locked=True).limit(1))
    return (copy, False) if copy else None


def create(db: Session, member: Member, titles: list[Title], *, collection_point: str, period_days: int,
           pay_fines: bool, pay_online: bool, now: datetime | None = None) -> Checkout:
    now = now or utcnow()
    cfg = settings_store.get_all(db)

    if period_days not in cfg["checkout_periods"]:
        raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                       {"periodDays": f"Choose one of: {', '.join(str(p) for p in cfg['checkout_periods'])}"})
    if open_checkout(db, member.id):
        existing = open_checkout(db, member.id)
        raise ApiError(409, "CONFLICT", "You already have an unpaid checkout. Pay for it or cancel it first.",
                       {"checkoutId": existing.id})

    for b in circ.compute_blocks(db, member, cfg, now, for_issue=False):
        if b["code"] == "fines":
            continue  # handled below: the member may choose to pay them now
        raise ApiError(403 if not b["overridable"] else 409, "FORBIDDEN" if not b["overridable"] else "CONFLICT",
                       b["message"])

    loans = circ.open_loans(db, member.id)
    on_loan = {l.copy.title_id for l in loans}
    dupes = [t.title for t in titles if t.id in on_loan]
    if dupes:
        raise ApiError(409, "CONFLICT", f"You already have '{dupes[0]}' on loan.")
    limit = cfg["max_items"].get(member.member_type, 1)
    if len(loans) + len(titles) > limit:
        raise ApiError(409, "LOAN_LIMIT", f"You can borrow up to {limit} item(s) and already have {len(loans)} on loan.")

    summary = fines.summary(db, member, now)
    owed_kobo = summary["outstandingKobo"]
    will_pay = pay_fines and pay_online and owed_kobo >= cfg["payment_min_kobo"]
    threshold = cfg["fine_block_threshold"]
    if owed_kobo // 100 > threshold and not will_pay:
        raise ApiError(409, "FINES_BLOCK", f"New loans are blocked until fines are below {circ.fmt_naira(threshold)}.")

    reserved, missing = [], []
    for t in titles:
        pick = _pick_copy(db, t, member)
        if not pick:
            missing.append(t.title)
            continue
        copy, from_hold = pick
        reserved.append({"copy_id": copy.id, "title_id": t.id, "hold": from_hold})
    if missing:
        raise ApiError(409, "NO_COPY_AVAILABLE", f"No copy of '{missing[0]}' is available right now.",
                       {"items": ", ".join(missing)})
    for r in reserved:
        if not r["hold"]:
            db.get(Copy, r["copy_id"]).status = "reserved"

    checkout = Checkout(id=new_checkout_id(db), member_id=member.id, items=[t.id for t in titles],
                        reserved_copy_ids=reserved, collection_point=collection_point, period_days=period_days,
                        status="awaiting_payment", expires_at=now + timedelta(minutes=cfg["checkout_hold_minutes"]),
                        created_at=now)
    db.add(checkout)
    db.flush()

    if will_pay:
        if not member.email:
            raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                           {"email": "Add an email address to your profile before paying online."})
        payment = payments.create_payment(
            db, member=member, amount_kobo=owed_kobo, email=member.email, purpose="Library fines payment",
            metadata={"kind": "fine_payment", "member": member.id_number, "checkout": checkout.id},
            checkout_id=checkout.id)
        checkout.fines_to_pay_kobo, checkout.payment_reference = owed_kobo, payment.reference
        audit(db, member, "checkout.create", "checkout", checkout.id,
              after={"items": checkout.items, "finesToPayKobo": owed_kobo, "payment": payment.reference})
    else:
        audit(db, member, "checkout.create", "checkout", checkout.id, after={"items": checkout.items})
        confirm(db, checkout, now)
    return checkout


def confirm(db: Session, checkout: Checkout, now: datetime | None = None) -> list[Loan]:
    """Turn the held copies into loans. All or nothing: a failure rolls back every loan made here."""
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    member = db.get(Member, checkout.member_id)
    made = []
    try:
        with db.begin_nested():
            for r in checkout.reserved_copy_ids:
                copy = db.get(Copy, r["copy_id"])
                if not r["hold"]:
                    copy.status = "available"  # circ.issue takes it from here
                loan = circ.issue(db, member, copy, member, now=now)
                loan.collection_point, loan.checkout_id = checkout.collection_point, checkout.id
                loan.due_at = circ.compute_due(now, member.member_type, copy.collection,
                                               {**cfg, "loan_days": {member.member_type: checkout.period_days}})
                made.append(loan)
            checkout.status = "confirmed"
    except circ.RuleError as e:
        raise ApiError(409, "CONFLICT", e.message) from None
    audit(db, member, "checkout.confirm", "checkout", checkout.id, after={"loans": [l.id for l in made]})
    return made


def _release(db: Session, checkout: Checkout, now: datetime) -> None:
    cfg = settings_store.get_all(db)
    for r in checkout.reserved_copy_ids:
        if r["hold"]:
            continue  # that copy belongs to the member's hold, not to this checkout
        copy = db.get(Copy, r["copy_id"])
        if copy and copy.status == "reserved":
            circ.release_copy(db, copy, now, cfg)


def cancel(db: Session, checkout: Checkout, actor: Member, now: datetime | None = None) -> None:
    now = now or utcnow()
    if checkout.status not in OPEN_STATUSES:
        raise ApiError(409, "CONFLICT", f"This checkout is {checkout.status.replace('_', ' ')} and cannot be cancelled.")
    _release(db, checkout, now)
    checkout.status = "cancelled"
    pay = db.scalar(select(Payment).where(Payment.reference == checkout.payment_reference)) \
        if checkout.payment_reference else None
    if pay and pay.status in ("pending", "failed"):
        pay.status = "abandoned"
        payments.add_event(db, pay, "Checkout cancelled")
    audit(db, actor, "checkout.cancel", "checkout", checkout.id)


def expire_stale(db: Session, now: datetime | None = None, checkout: Checkout | None = None) -> int:
    """Checkouts that sat unpaid past the hold window lose their copies. A payment that arrives afterwards is
    credited to the member rather than lost (see payments.apply_success)."""
    now = now or utcnow()
    stmt = select(Checkout).where(Checkout.status.in_(OPEN_STATUSES), Checkout.expires_at < now)
    if checkout is not None:
        stmt = stmt.where(Checkout.id == checkout.id)
    rows = list(db.scalars(stmt))
    for c in rows:
        _release(db, c, now)
        c.status = "expired"
        audit(db, None, "checkout.expire", "checkout", c.id, actor_label="system")
    return len(rows)


def expire_job() -> int:
    with SessionLocal() as db:
        n = expire_stale(db)
        db.commit()
        return n
