"""What happens to a payment, and to the fines and checkout behind it, when the provider reports an outcome.

Everything funnels through apply_result(): the verify endpoint and the webhook handler both call it, so a payment
is settled by exactly one piece of code and only ever from a provider-verified result."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..apierrors import ApiError
from ..models import Checkout, FinePayment, LedgerEntry, Member, Payment
from ..timeutil import utcnow
from . import checkout as checkout_svc
from . import payments
from .audit import audit
from .circulation import fmt_naira
from .notifications import notify
from .providers import get_provider

ABANDON_AFTER = timedelta(hours=24)


def _alert_librarians(db: Session, template: str, payment: Payment, member: Member | None, **ctx) -> None:
    staff = db.scalars(select(Member).where(Member.role.in_(["head_librarian", "circulation"]),
                                            Member.status == "active"))
    for s in staff:
        notify(db, s, template, {"reference": payment.reference, "amount": fmt_naira(payment.amount_kobo // 100),
                                 "member": member.id_number if member else "unknown", **ctx},
               dedupe_key=f"{template}:{payment.reference}:{s.id}")


def apply_result(db: Session, payment: Payment, result: dict, now: datetime | None = None) -> bool:
    """Settle a payment from a verified provider result. Returns True if anything changed.
    Calling it again with the same result changes nothing."""
    now = now or utcnow()
    status = result.get("status")
    if payment.status == "refunded":
        return False

    if status == "success":
        if payment.status == "success":
            return False
        payment.status = "success"
        payment.channel = result.get("channel") or payment.channel
        payment.fee_kobo = result.get("feeKobo") or 0
        payment.paid_at = result.get("paidAt") or now
        payment.gateway_response = result.get("message") or "Approved"
        payments.add_event(db, payment, f"Payment successful ({payment.channel or 'unknown channel'})")
        _settle_money(db, payment, now)
        audit(db, None, "payment.success", "payment", payment.reference, actor_label="system",
              after={"amountKobo": payment.amount_kobo, "feeKobo": payment.fee_kobo, "channel": payment.channel})
        return True

    if status == "failed":
        if payment.status != "pending":  # never downgrade a success; a failed payment is already failed
            return False
        payment.status = "failed"
        payment.gateway_response = result.get("message") or "Declined"
        payments.add_event(db, payment, f"Payment failed: {payment.gateway_response}")
        checkout = db.get(Checkout, payment.checkout_id) if payment.checkout_id else None
        if checkout and checkout.status == "awaiting_payment":
            checkout.status = "payment_failed"  # copies stay held until the checkout expires
        audit(db, None, "payment.failed", "payment", payment.reference, actor_label="system",
              after={"message": payment.gateway_response})
        return True
    return False


def _settle_money(db: Session, payment: Payment, now: datetime) -> None:
    member = db.get(Member, payment.member_id) if payment.member_id else None
    checkout = db.get(Checkout, payment.checkout_id) if payment.checkout_id else None
    meta = payment.payment_metadata or {}
    if member and (checkout or meta.get("kind") == "fine_payment"):
        naira = payment.amount_kobo // 100
        entry = LedgerEntry(member_id=member.id, kind="payment", amount=naira, method="online",
                            receipt_no=payment.reference, note=f"Online payment {payment.reference}",
                            created_at=now, updated_at=now)
        db.add(entry)
        db.flush()
        db.add(FinePayment(payment_id=payment.id, ledger_id=entry.id, amount_kobo=naira * 100))
        payments.add_event(db, payment, f"Credited {fmt_naira(naira)} to the member's account")
    if not checkout:
        return
    if checkout.status in checkout_svc.OPEN_STATUSES:
        try:
            checkout_svc.confirm(db, checkout, now)
            payments.add_event(db, payment, "Checkout confirmed, loans issued")
        except ApiError as e:
            checkout_svc._release(db, checkout, now)
            checkout.status = "expired"
            payments.add_event(db, payment, f"Checkout could not be completed: {e.message}")
            _alert_librarians(db, "checkout_failed", payment, member, reason=e.message)
    else:  # the hold window ran out before the money arrived: keep the credit, issue nothing, tell a librarian
        payments.add_event(db, payment, f"Arrived after the checkout {checkout.status}; kept as account credit")
        _alert_librarians(db, "payment_late", payment, member)


def mark_refunded(db: Session, payment: Payment, actor: Member | None = None, now: datetime | None = None) -> bool:
    """Only a successful payment can be refunded. The fines it covered become owed again."""
    now = now or utcnow()
    if payment.status != "success":
        return False
    payment.status = "refunded"
    covered = db.scalar(select(func.coalesce(func.sum(FinePayment.amount_kobo), 0))
                        .where(FinePayment.payment_id == payment.id)) or 0
    member = db.get(Member, payment.member_id) if payment.member_id else None
    if member and covered:
        db.add(LedgerEntry(member_id=member.id, kind="adjustment", amount=covered // 100, officer_id=actor.id if actor else None,
                           note=f"Refund of {payment.reference}: fines reopened", created_at=now, updated_at=now))
    payments.add_event(db, payment, "Refunded")
    audit(db, actor, "payment.refund", "payment", payment.reference, actor_label="system",
          after={"amountKobo": payment.amount_kobo, "reopenedKobo": covered})
    if member:
        notify(db, member, "payment_refunded", {"reference": payment.reference,
                                                 "amount": fmt_naira(payment.amount_kobo // 100)},
               dedupe_key=f"refund:{payment.reference}")
    return True


def reopen(db: Session, payment: Payment) -> None:
    """A failed payment may be retried on the same reference."""
    if payment.status != "failed":
        return
    payment.status, payment.gateway_response = "pending", None
    payments.add_event(db, payment, "Retry started")
    checkout = db.get(Checkout, payment.checkout_id) if payment.checkout_id else None
    if checkout and checkout.status == "payment_failed":
        checkout.status = "awaiting_payment"
    provider = get_provider(db, payment.provider)
    if hasattr(provider, "reset"):
        provider.reset(payment)


def abandon_job() -> int:
    from ..database import SessionLocal
    with SessionLocal() as db:
        n = abandon_stale(db)
        db.commit()
        return n


def abandon_stale(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    rows = list(db.scalars(select(Payment).where(Payment.status == "pending",
                                                 Payment.updated_at < now - ABANDON_AFTER)))
    for p in rows:
        p.status = "abandoned"
        payments.add_event(db, p, "Abandoned: no activity for 24 hours")
    return len(rows)
