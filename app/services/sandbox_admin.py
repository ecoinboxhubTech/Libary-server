"""Gateway-dashboard numbers and the sandbox reset."""
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (Checkout, FinePayment, LedgerEntry, Member, Payment, PaymentEvent, SandboxCharge,
                      SandboxTransfer, WebhookEvent)
from . import checkout as checkout_svc
from . import payments
from .audit import audit
from .providers.sandbox import SandboxProvider


def stats(db: Session) -> dict:
    def total(col, *status):
        return db.scalar(select(func.coalesce(func.sum(col), 0)).where(Payment.provider == "sandbox",
                                                                      Payment.status.in_(status))) or 0

    def count(*status):
        return db.scalar(select(func.count()).select_from(Payment).where(Payment.provider == "sandbox",
                                                                         Payment.status.in_(status))) or 0
    gross, fees = total(Payment.amount_kobo, "success"), total(Payment.fee_kobo, "success")
    ok, bad = count("success"), count("failed")
    return {"grossKobo": gross, "feesKobo": fees, "netKobo": gross - fees, "success": ok, "failed": bad,
            "refundedKobo": total(Payment.amount_kobo, "refunded"),
            "ratePercent": round(100 * ok / (ok + bad), 1) if ok + bad else 0}


SAMPLES = [  # amount (kobo), outcome, channel, message
    (20000, "success", "card", None),
    (350000, "success", "bank_transfer", None),
    (150000, "failed", "card", "Insufficient funds"),
    (50000, "refunded", "card", None),
]


def seed_samples(db: Session) -> None:
    for amount, outcome, channel, message in SAMPLES:
        p = payments.create_payment(db, member=None, amount_kobo=amount, email="sample@student.nitt.edu.ng",
                                    purpose="Sample library fine", metadata={"sample": True})
        provider = SandboxProvider(db)
        provider.settle(p, "failed" if outcome == "failed" else "success", channel=channel, message=message)
        if outcome == "refunded":
            provider.refund(p.reference)


def reset(db: Session, actor: Member) -> dict:
    """Delete every sandbox payment and what hangs off it (including the ledger rows those payments wrote),
    then put a few sample transactions back. Open checkouts are cancelled first so their copies go back on the shelf."""
    refs = list(db.scalars(select(Payment.reference).where(Payment.provider == "sandbox")))
    ids = list(db.scalars(select(Payment.id).where(Payment.provider == "sandbox")))
    if refs:
        for c in db.scalars(select(Checkout).where(Checkout.payment_reference.in_(refs),
                                                   Checkout.status.in_(checkout_svc.OPEN_STATUSES))):
            checkout_svc.cancel(db, c, actor)
        for c in db.scalars(select(Checkout).where(Checkout.payment_reference.in_(refs))):
            c.payment_reference = None
        db.flush()
        db.execute(delete(FinePayment).where(FinePayment.payment_id.in_(ids)))
        db.execute(delete(LedgerEntry).where(LedgerEntry.kind == "payment", LedgerEntry.receipt_no.in_(refs)))
        for ref in refs:
            db.execute(delete(LedgerEntry).where(LedgerEntry.kind == "adjustment",
                                                 LedgerEntry.note.like(f"Refund of {ref}:%")))
        db.execute(delete(PaymentEvent).where(PaymentEvent.payment_id.in_(ids)))
        db.execute(delete(SandboxTransfer).where(SandboxTransfer.payment_id.in_(ids)))
        db.execute(delete(SandboxCharge).where(SandboxCharge.reference.in_(refs)))
        db.execute(delete(Payment).where(Payment.id.in_(ids)))
    removed_hooks = db.execute(delete(WebhookEvent).where(WebhookEvent.provider == "sandbox")).rowcount
    audit(db, actor, "sandbox.reset", "payment", None, after={"payments": len(refs), "webhooks": removed_hooks})
    seed_samples(db)
    return {"deletedPayments": len(refs), "deletedWebhooks": removed_hooks, "seeded": len(SAMPLES)}
