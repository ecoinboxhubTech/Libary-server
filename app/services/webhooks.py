"""Inbound webhooks: verify the signature on the raw body, store once, then settle the payment.

receive() and process_event() are shared by the public endpoint and by the sandbox, which sends itself the same
signed events a real provider would."""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..database import SessionLocal
from ..models import Payment, WebhookEvent
from ..timeutil import utcnow
from . import payment_flow
from .providers import get_provider, sign
from .providers.sandbox import SIGNATURE_HEADER

log = logging.getLogger("library.webhooks")
RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30)]  # then hourly
GIVE_UP_AFTER = timedelta(hours=24)


def receive(db: Session, provider_name: str, raw: bytes, headers: dict) -> tuple[int, dict, WebhookEvent | None]:
    """Returns (http status, body, event to process or None). A bad signature changes nothing."""
    provider = get_provider(db, provider_name)
    parsed = provider.parse_webhook(raw, headers)
    if not parsed["valid"]:
        return (400, {"status": "malformed"}, None) if parsed.get("malformed") else (401, {"status": "invalid signature"}, None)
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.provider == provider_name,
                                                    WebhookEvent.event_id == parsed["event_id"]))
    if existing:
        existing.attempts += 1  # a duplicate delivery: acknowledged, never processed twice
        return 200, {"status": "duplicate"}, None
    sig = next((v for k, v in headers.items() if k.lower().endswith("signature")), "") or ""
    ev = WebhookEvent(provider=provider_name, event_id=parsed["event_id"], event=parsed["event"],
                      reference=parsed["reference"], payload=parsed["data"], signature=sig[:16] + "…" if sig else None,
                      http_status=200, attempts=1)
    db.add(ev)
    db.flush()
    return 200, {"status": "received"}, ev


def _handle(db: Session, ev: WebhookEvent) -> None:
    payment = db.scalar(select(Payment).where(Payment.reference == ev.reference)) if ev.reference else None
    if not payment:
        return  # not ours; acknowledge so the sender stops retrying
    if ev.event in ("charge.success", "charge.failed"):
        # The webhook only says "look at this payment". The outcome is what the provider says when we ask.
        payment_flow.apply_result(db, payment, get_provider(db, payment.provider).verify(payment.reference))
    elif ev.event == "refund.processed":
        payment_flow.mark_refunded(db, payment)


def process_event(db: Session, ev: WebhookEvent, now: datetime | None = None) -> bool:
    """Run the handler in a savepoint so a failure leaves no half-applied payment, and schedule a retry."""
    now = now or utcnow()
    try:
        with db.begin_nested():
            _handle(db, ev)
    except Exception:  # noqa: BLE001 - any failure is retried later; details go to the log, never the payload
        log.exception("Webhook %s (%s) failed", ev.event_id, ev.event)
        waited = now - ev.received_at
        step = min(ev.attempts - 1, len(RETRY_DELAYS))
        delay = RETRY_DELAYS[step] if step < len(RETRY_DELAYS) else timedelta(hours=1)
        ev.http_status = 500
        ev.next_attempt_at = None if waited + delay > GIVE_UP_AFTER else now + delay
        return False
    ev.processed_at, ev.http_status, ev.next_attempt_at = now, 200, None
    return True


def retry_due(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    done = 0
    for ev in db.scalars(select(WebhookEvent).where(WebhookEvent.processed_at.is_(None),
                                                    WebhookEvent.next_attempt_at.isnot(None),
                                                    WebhookEvent.next_attempt_at <= now)):
        ev.attempts += 1
        done += process_event(db, ev, now)
    return done


def retry_job() -> int:
    with SessionLocal() as db:
        n = retry_due(db)
        db.commit()
        return n


def run_event(event_pk: int) -> None:
    """Background task for the public endpoint: settle in its own session after the 200 has gone out."""
    with SessionLocal() as db:
        ev = db.get(WebhookEvent, event_pk)
        if ev and ev.processed_at is None:
            process_event(db, ev)
            db.commit()


# ------------------------------------------------------------------ sandbox emission
def build_payload(payment: Payment, event: str, event_id: str, charge=None) -> dict:
    """`charge` is the sandbox's own record; it is ahead of the local payment row, which only changes on verify."""
    status = {"charge.success": "success", "charge.failed": "failed", "refund.processed": "refunded"}[event]
    return {"id": event_id, "event": event, "data": {
        "reference": payment.reference, "amount": payment.amount_kobo, "currency": payment.currency, "status": status,
        "channel": charge.channel if charge else payment.channel, "customer": {"email": payment.email},
        "metadata": payment.payment_metadata or {},
        "paid_at": (charge.paid_at.isoformat() + "Z") if charge and charge.paid_at else None,
        "fees": charge.fee_kobo if charge else payment.fee_kobo}}


def emit_sandbox(db: Session, payment: Payment, event: str, charge=None) -> WebhookEvent | None:
    """The sandbox 'sends' a signed webhook to our own receiver and we process it in-line."""
    import secrets
    payload = build_payload(payment, event, "evt_" + secrets.token_hex(8), charge)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = {SIGNATURE_HEADER: sign(raw, get_config().webhook_signing_key)}
    status, _, ev = receive(db, "sandbox", raw, headers)
    if ev:
        process_event(db, ev)
    return ev
