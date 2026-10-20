"""NITT Pay sandbox (brief section 8): the endpoints behind the hosted pay page and the /gateway dashboard.

Only enabled when PAYMENT_PROVIDER=sandbox. The customer endpoints are public, like a real provider's hosted page:
the payment reference is the only credential. Card numbers are validated in memory and thrown away."""
import hmac
import json
import random
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..apierrors import ApiError
from ..config import get_config
from ..database import get_db
from ..models import Member, Payment, SandboxCharge, SandboxTransfer, WebhookEvent
from ..security import require_roles
from ..serializers import iso
from ..services import payment_flow, payments, sandbox_admin, sandbox_cards, webhooks
from ..services.audit import audit
from ..services.providers import sign
from ..services.providers.sandbox import SIGNATURE_HEADER, SandboxProvider
from ..timeutil import utcnow
from .errors import not_found, paged
from .payments import card

TRANSFER_MINUTES = 30


def sandbox_only() -> None:
    if get_config().payment_provider != "sandbox":
        raise ApiError(404, "NOT_FOUND", "The sandbox is not enabled.")


customer = APIRouter(prefix="/sandbox", tags=["sandbox"], dependencies=[Depends(sandbox_only)])
admin = APIRouter(prefix="/sandbox/admin", tags=["sandbox-admin"], dependencies=[Depends(sandbox_only)])
FINANCE = require_roles("finance", "admin")


def pause() -> None:
    if get_config().sandbox_delay:
        time.sleep(random.uniform(0.5, 1.5))


def _payment(db: Session, ref: str) -> Payment:
    p = db.scalar(select(Payment).where(Payment.reference == ref, Payment.provider == "sandbox"))
    if not p:
        raise not_found("Transaction not found.")
    return p


def _charge(db: Session, p: Payment) -> SandboxCharge:
    c = db.get(SandboxCharge, p.reference)
    if not c:
        c = SandboxCharge(reference=p.reference)
        db.add(c)
        db.flush()
    return c


def _payable(db: Session, p: Payment) -> None:
    """A finished payment cannot be paid again; a failed one is reopened so the customer can try once more."""
    if p.status in ("success", "refunded", "abandoned"):
        raise ApiError(409, "CONFLICT", f"This payment is {p.status} and cannot be paid.")
    if p.status == "failed":
        payment_flow.reopen(db, p)


def _transfer_out(t: SandboxTransfer) -> dict:
    return {"rrr": t.rrr, "bank": t.bank, "account": t.account_number, "accountName": t.account_name,
            "expires": iso(t.expires_at)}


# ------------------------------------------------------------------ customer
@customer.get("/transactions/{ref}")
def fetch(ref: str, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    c = db.get(SandboxCharge, p.reference)
    t = db.get(SandboxTransfer, p.id)
    return {
        "reference": p.reference, "amountKobo": p.amount_kobo, "currency": p.currency, "purpose": p.purpose,
        "email": p.email, "status": p.status, "channel": p.channel, "gatewayResponse": p.gateway_response,
        "merchant": "NITT Library", "awaitingOtp": bool(c and c.awaiting_otp and p.status == "pending"),
        "attemptsLeft": max(0, sandbox_cards.MAX_CARD_ATTEMPTS - (c.card_attempts if c else 0)),
        "transfer": _transfer_out(t) if t else None,
    }


class CardIn(BaseModel):
    # repr=False keeps these out of any log line that prints the model
    number: str = Field(max_length=40, repr=False)
    exp: str = Field(max_length=10, repr=False)
    cvv: str = Field(max_length=10, repr=False)


@customer.post("/transactions/{ref}/card")
def charge_card(ref: str, body: CardIn, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    _payable(db, p)
    charge = _charge(db, p)
    if charge.card_attempts >= sandbox_cards.MAX_CARD_ATTEMPTS:
        raise ApiError(429, "RATE_LIMITED", "Too many card attempts for this payment. Please start again.")
    charge.card_attempts += 1
    left = sandbox_cards.MAX_CARD_ATTEMPTS - charge.card_attempts
    digits, problem = sandbox_cards.validate(body.number, body.exp, body.cvv, utcnow())
    if problem:  # a typo is not a declined payment: the payment stays open, but the attempt counts
        db.commit()
        return {"status": "fail", "message": problem, "attemptsLeft": left}
    pause()
    result, message = sandbox_cards.outcome(digits)
    del digits  # the number is not kept past this point
    provider = SandboxProvider(db)
    if result == "otp":
        charge.awaiting_otp, charge.channel, charge.otp_attempts = True, "card", 0
        payments.add_event(db, p, "OTP requested")
        db.commit()
        return {"status": "otp", "message": "Enter the one-time code sent to your phone.", "attemptsLeft": left}
    provider.settle(p, "success" if result == "success" else "failed", channel="card", message=message)
    db.commit()
    return {"status": result, "message": message or "Payment successful", "attemptsLeft": left}


class OtpIn(BaseModel):
    otp: str = Field(max_length=12, repr=False)


@customer.post("/transactions/{ref}/otp")
def submit_otp(ref: str, body: OtpIn, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    charge = db.get(SandboxCharge, p.reference)
    if p.status != "pending" or not charge or not charge.awaiting_otp:
        raise ApiError(409, "CONFLICT", "No one-time code is pending for this payment.")
    pause()
    provider = SandboxProvider(db)
    if hmac.compare_digest(body.otp.strip(), sandbox_cards.OTP_CODE):
        provider.settle(p, "success", channel="card")
        db.commit()
        return {"status": "success", "message": "Payment successful"}
    charge.otp_attempts += 1
    if charge.otp_attempts >= sandbox_cards.MAX_OTP_ATTEMPTS:
        provider.settle(p, "failed", channel="card", message="Too many incorrect OTP attempts")
        db.commit()
        return {"status": "fail", "message": "Too many incorrect OTP attempts"}
    db.commit()
    return {"status": "otp", "message": f"Incorrect code. {sandbox_cards.MAX_OTP_ATTEMPTS - charge.otp_attempts} attempt(s) left."}


@customer.post("/transactions/{ref}/transfer")
def start_transfer(ref: str, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    _payable(db, p)
    now = utcnow()
    t = db.get(SandboxTransfer, p.id)
    if not t or t.expires_at <= now:  # a live transfer is reused; an expired one is replaced
        details = {"rrr": "".join(secrets.choice("0123456789") for _ in range(12)), "bank": "NITT Pay Test Bank",
                   "account_number": "99" + "".join(secrets.choice("0123456789") for _ in range(8)),
                   "account_name": f"NITT LIBRARY / {p.reference}", "expires_at": now + timedelta(minutes=TRANSFER_MINUTES)}
        if t:
            for k, v in details.items():
                setattr(t, k, v)
        else:
            t = SandboxTransfer(payment_id=p.id, **details)
            db.add(t)
        payments.add_event(db, p, "Bank transfer started")
    _charge(db, p).channel = "bank_transfer"
    db.commit()
    return _transfer_out(t)


@customer.post("/transactions/{ref}/transfer/confirm")
def confirm_transfer(ref: str, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    t = db.get(SandboxTransfer, p.id)
    if not t:
        raise ApiError(409, "CONFLICT", "No bank transfer has been started for this payment.")
    if p.status == "success":
        return {"status": "success", "message": "Payment successful"}
    if p.status != "pending":
        return {"status": "fail", "message": p.gateway_response or f"Payment is {p.status}"}
    pause()
    provider = SandboxProvider(db)
    if utcnow() > t.expires_at:
        provider.settle(p, "failed", channel="bank_transfer", message="Transfer window expired")
        db.commit()
        return {"status": "fail", "message": "Transfer window expired"}
    provider.settle(p, "success", channel="bank_transfer")
    db.commit()
    return {"status": "success", "message": "Payment successful"}


@customer.post("/transactions/{ref}/transfer/expire")
def expire_transfer(ref: str, db: Session = Depends(get_db)):
    p = _payment(db, ref)
    t = db.get(SandboxTransfer, p.id)
    if not t:
        raise ApiError(409, "CONFLICT", "No bank transfer has been started for this payment.")
    if p.status != "pending":
        raise ApiError(409, "CONFLICT", f"This payment is {p.status}.")
    t.expires_at = utcnow()
    SandboxProvider(db).settle(p, "failed", channel="bank_transfer", message="Transfer expired")
    db.commit()
    return {"status": "fail", "message": "Transfer expired"}


# ------------------------------------------------------------------ admin (finance / admin)
@admin.get("/stats")
def stats(_: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    return sandbox_admin.stats(db)


@admin.get("/transactions")
def transactions(status: str | None = Query(None, pattern="^(pending|success|failed|refunded|abandoned)$"),
                 page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                 _: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    where = [Payment.provider == "sandbox"] + ([Payment.status == status] if status else [])
    total = db.scalar(select(func.count()).select_from(Payment).where(*where))
    rows = db.scalars(select(Payment).where(*where).order_by(Payment.created_at.desc(), Payment.id.desc())
                      .offset((page - 1) * limit).limit(limit)).all()
    return paged([card(db, p) for p in rows], page, limit, total)


def _hook_row(e: WebhookEvent) -> dict:
    return {"id": e.id, "event": e.event, "reference": e.reference, "payload": e.payload, "signature": e.signature,
            "httpStatus": e.http_status, "attempts": e.attempts, "receivedAt": iso(e.received_at),
            "processedAt": iso(e.processed_at), "nextAttemptAt": iso(e.next_attempt_at)}


@admin.get("/webhooks")
def webhook_log(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                _: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    where = WebhookEvent.provider == "sandbox"
    total = db.scalar(select(func.count()).select_from(WebhookEvent).where(where))
    rows = db.scalars(select(WebhookEvent).where(where).order_by(WebhookEvent.id.desc())
                      .offset((page - 1) * limit).limit(limit)).all()
    return paged([_hook_row(e) for e in rows], page, limit, total)


@admin.post("/webhooks/{event_id}/resend")
def resend_webhook(event_id: int, actor: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    """Redeliver through the real receiver: signed, de-duplicated, attempts counted. An event that never finished
    processing is processed now."""
    ev = db.get(WebhookEvent, event_id)
    if not ev or ev.provider != "sandbox":
        raise not_found("Webhook event not found.")
    raw = json.dumps(ev.payload, separators=(",", ":")).encode()
    webhooks.receive(db, "sandbox", raw, {SIGNATURE_HEADER: sign(raw, get_config().webhook_signing_key)})
    if ev.processed_at is None:
        webhooks.process_event(db, ev)
    audit(db, actor, "sandbox.webhook.resend", "webhook", ev.id, after={"event": ev.event, "reference": ev.reference})
    db.commit()
    return _hook_row(ev)


@admin.get("/keys")
def keys(_: Member = Depends(FINANCE)):
    cfg = get_config()
    secret = cfg.payment_secret_key
    masked = "sk_test_" + "•" * 12 + (secret[-4:] if len(secret) >= 12 else "")
    return {"provider": "sandbox", "publicKey": cfg.payment_public_key, "secretKey": masked,
            "webhookUrl": "/api/v1/webhooks/payments/sandbox", "signatureHeader": SIGNATURE_HEADER}


class ResetIn(BaseModel):
    confirm: bool = False


@admin.post("/reset")
def reset(body: ResetIn, actor: Member = Depends(FINANCE), db: Session = Depends(get_db)):
    if get_config().app_env == "production":
        raise ApiError(403, "FORBIDDEN", "The sandbox cannot be reset in production.")
    if not body.confirm:
        raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                       {"confirm": "Send {\"confirm\": true}: this deletes every sandbox payment."})
    out = sandbox_admin.reset(db, actor)
    db.commit()
    return out
