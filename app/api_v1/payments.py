"""Payments and the public webhook receiver (brief sections 7 and 9)."""
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..apierrors import ApiError
from ..config import get_config
from ..database import get_db
from ..models import Checkout, Member, Payment, PaymentEvent
from ..security import current_member, require_roles
from ..serializers import iso
from ..services import payment_flow, payments, settings_store, webhooks
from ..services.audit import audit
from ..services.providers import get_provider
from . import idempotency
from .errors import not_found, paged

router = APIRouter()
FINANCE = require_roles("finance", "admin")
STAFF_VIEW = ("finance", "admin", "head_librarian", "circulation")
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def card(db: Session, p: Payment, *, events: bool = True) -> dict:
    out = {
        "reference": p.reference, "status": p.status, "amountKobo": p.amount_kobo, "feeKobo": p.fee_kobo,
        "netKobo": p.amount_kobo - p.fee_kobo if p.status in ("success", "refunded") else 0, "currency": p.currency,
        "channel": p.channel, "provider": p.provider, "purpose": p.purpose, "email": p.email,
        "checkoutId": p.checkout_id, "gatewayResponse": p.gateway_response, "createdAt": iso(p.created_at),
        "paidAt": iso(p.paid_at), "authorizationUrl": p.authorization_url,
    }
    if p.checkout_id:
        c = db.get(Checkout, p.checkout_id)
        out["checkoutStatus"] = c.status if c else None
    if events:
        rows = db.scalars(select(PaymentEvent).where(PaymentEvent.payment_id == p.id).order_by(PaymentEvent.id))
        out["events"] = [{"label": e.label, "createdAt": iso(e.created_at)} for e in rows]
    return out


def visible(db: Session, actor: Member, reference: str) -> Payment:
    p = db.scalar(select(Payment).where(Payment.reference == reference))
    if not p or (p.member_id != actor.id and actor.role not in STAFF_VIEW):
        raise not_found("Payment not found.")  # 404 for other people's payments: do not confirm they exist
    return p


class InitializeIn(BaseModel):
    amountKobo: int = Field(gt=0)
    email: str = Field(pattern=EMAIL, max_length=150)
    purpose: str = Field(min_length=1, max_length=120)
    metadata: dict = Field(default_factory=dict)


@router.post("/payments/initialize", status_code=201, tags=["payments"])
def initialize(body: InitializeIn, idempotency_key: str | None = Header(default=None, alias=idempotency.HEADER),
               actor: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    again = idempotency.replay(db, actor, idempotency_key, "POST /payments/initialize")
    if again:
        return again
    cfg = settings_store.get_all(db)
    if body.amountKobo < cfg["payment_min_kobo"]:
        raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                       {"amountKobo": f"The minimum payment is {cfg['payment_min_kobo']} kobo."})
    member = None
    if body.metadata.get("kind") == "fine_payment":  # a manual link that clears someone's fines
        member = db.scalar(select(Member).where(Member.id_number == str(body.metadata.get("member", "")).upper()))
        if not member:
            raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.", {"metadata.member": "Unknown member."})
        if body.amountKobo % 100:
            raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                           {"amountKobo": "Fine payments must be whole naira."})
    p = payments.create_payment(db, member=member, amount_kobo=body.amountKobo, email=body.email,
                                purpose=body.purpose, metadata=body.metadata)
    audit(db, actor, "payment.initialize", "payment", p.reference, after={"amountKobo": p.amount_kobo})
    out = {"reference": p.reference, "authorizationUrl": p.authorization_url}
    idempotency.remember(db, actor, idempotency_key, "POST /payments/initialize", 201, out)
    db.commit()
    return JSONResponse(out, status_code=201)


@router.get("/payments/{reference}", tags=["payments"])
def get_payment(reference: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    return card(db, visible(db, actor, reference))


@router.post("/payments/{reference}/verify", tags=["payments"])
def verify_payment(reference: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    """Asks the provider for the final word, then settles the payment, fines and checkout in one transaction.
    Safe to call repeatedly. This is the only thing the browser's return page triggers."""
    p = visible(db, actor, reference)
    result = get_provider(db, p.provider).verify(p.reference)
    payment_flow.apply_result(db, p, result)
    db.commit()
    return card(db, p)


@router.get("/me/payments", tags=["payments"])
def my_payments(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    where = Payment.member_id == actor.id
    total = db.scalar(select(func.count()).select_from(Payment).where(where))
    rows = db.scalars(select(Payment).where(where).order_by(Payment.created_at.desc(), Payment.id.desc())
                      .offset((page - 1) * limit).limit(limit)).all()
    return paged([card(db, p, events=False) for p in rows], page, limit, total)


class RefundIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/payments/{reference}/refund", tags=["payments"])
def refund_payment(reference: str, body: RefundIn | None = None, actor: Member = Depends(FINANCE),
                   db: Session = Depends(get_db)):
    p = db.scalar(select(Payment).where(Payment.reference == reference))
    if not p:
        raise not_found("Payment not found.")
    if p.status != "success":
        raise ApiError(409, "CONFLICT", f"Only a successful payment can be refunded; this one is {p.status}.")
    get_provider(db, p.provider).refund(p.reference)
    payment_flow.mark_refunded(db, p, actor)  # no-op if the provider's webhook already did it
    audit(db, actor, "payment.refund.request", "payment", p.reference,
          after={"reason": body.reason if body else None, "amountKobo": p.amount_kobo})
    db.commit()
    return card(db, p)


# ------------------------------------------------------------------ webhooks
@router.post("/webhooks/payments/{provider}", tags=["webhooks"])
async def receive_webhook(provider: str, request: Request, background: BackgroundTasks,
                          db: Session = Depends(get_db)):
    """Public. The signature is checked against the raw bytes before anything is parsed or stored."""
    if provider != get_config().payment_provider:
        raise not_found("Unknown provider.")
    raw = await request.body()
    status, body, ev = webhooks.receive(db, provider, raw, dict(request.headers))
    db.commit()
    if ev:
        background.add_task(webhooks.run_event, ev.id)  # answer fast; settle after the 200 has gone out
    return JSONResponse(body, status_code=status)
