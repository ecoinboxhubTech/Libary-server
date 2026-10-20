"""Payment providers behind one interface. Switch with PAYMENT_PROVIDER=sandbox|paystack|remita.

    initialize(reference, amount_kobo, email, metadata) -> {"authorizationUrl": str}
    verify(reference)  -> {"status": "pending"|"success"|"failed", "channel", "feeKobo", "paidAt", "message", "raw"}
    refund(reference)  -> {"status": "refunded"}
    parse_webhook(raw_body, headers) -> {"valid", "event", "reference", "event_id", "data"}

`parse_webhook` must check the signature against the raw bytes, before any JSON parsing, and in constant time."""
import hashlib
import hmac
import json
from typing import Protocol

from sqlalchemy.orm import Session

from ...apierrors import ApiError
from ...config import get_config


class PaymentProvider(Protocol):
    name: str

    def initialize(self, *, reference: str, amount_kobo: int, email: str, metadata: dict) -> dict: ...
    def verify(self, reference: str) -> dict: ...
    def refund(self, reference: str) -> dict: ...
    def parse_webhook(self, raw_body: bytes, headers: dict) -> dict: ...


def sign(raw: bytes, key: str) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()


def signature_ok(raw: bytes, given: str | None, key: str) -> bool:
    if not key or not given:
        return False
    return hmac.compare_digest(sign(raw, key), given.strip().lower())


def parse_json_event(raw: bytes, valid: bool) -> dict:
    """Shared shape for a webhook body of {"id"?, "event", "data": {"reference", ...}}."""
    out = {"valid": valid, "event": None, "reference": None, "event_id": None, "data": {}}
    if not valid:
        return out
    try:
        body = json.loads(raw)
        data = body.get("data") or {}
        out.update(event=body["event"], reference=data.get("reference"), data=body,
                   event_id=str(body.get("id") or hashlib.sha256(raw).hexdigest()))
    except (ValueError, KeyError, AttributeError):
        out["valid"] = False
        out["malformed"] = True
    return out


def lower_headers(headers) -> dict:
    return {k.lower(): v for k, v in dict(headers).items()}


def get_provider(db: Session, name: str | None = None) -> PaymentProvider:
    name = (name or get_config().payment_provider).lower()
    if name == "sandbox":
        from .sandbox import SandboxProvider
        return SandboxProvider(db)
    if name == "paystack":
        from .paystack import PaystackProvider
        return PaystackProvider(db)
    if name == "remita":
        from .remita import RemitaProvider
        return RemitaProvider(db)
    raise ApiError(404, "NOT_FOUND", "Unknown payment provider.")
