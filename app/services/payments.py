"""Payment records. The server owns every amount; nothing here trusts a figure sent by the browser."""
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Member, Payment, PaymentEvent
from . import settings_store
from .providers import get_provider

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I, so a reference can be read out over the phone


def new_reference(db: Session) -> str:
    while True:
        ref = "NITT_" + "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if not db.scalar(select(Payment.id).where(Payment.reference == ref)):
            return ref


def add_event(db: Session, payment: Payment, label: str) -> None:
    db.add(PaymentEvent(payment_id=payment.id, label=label))


def compute_fee(db: Session, amount_kobo: int) -> int:
    """Percentage fee, plus a flat amount from the threshold up, capped. Rates are settings, not constants."""
    cfg = settings_store.get_all(db)
    fee = round(amount_kobo * cfg["payment_fee_percent"] / 100)
    if amount_kobo >= cfg["payment_fee_threshold_kobo"]:
        fee += cfg["payment_fee_flat_kobo"]
    return min(fee, cfg["payment_fee_cap_kobo"])


def authorization_url_for(db: Session, reference: str) -> str | None:
    return db.scalar(select(Payment.authorization_url).where(Payment.reference == reference))


def create_payment(db: Session, *, member: Member | None, amount_kobo: int, email: str, purpose: str,
                   metadata: dict | None = None, checkout_id: str | None = None) -> Payment:
    provider = get_config().payment_provider
    p = Payment(reference=new_reference(db), member_id=member.id if member else None, checkout_id=checkout_id,
                amount_kobo=amount_kobo, currency="NGN", status="pending", provider=provider,
                purpose=purpose, email=email, payment_metadata=metadata or {})
    db.add(p)
    db.flush()
    started = get_provider(db, provider).initialize(reference=p.reference, amount_kobo=amount_kobo, email=email,
                                                    metadata=p.payment_metadata)
    p.authorization_url = started["authorizationUrl"]
    add_event(db, p, "Payment created")
    db.flush()  # autoflush is off; later reads in this request must see the URL
    return p
