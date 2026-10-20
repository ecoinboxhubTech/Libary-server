"""NITT Pay sandbox: a stand-in for a real gateway. It keeps its own charge records (SandboxCharge) and tells the
library about outcomes by sending signed webhooks, exactly as a real provider would."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_config
from ...models import Payment, SandboxCharge
from ...timeutil import utcnow
from . import lower_headers, parse_json_event, signature_ok

SIGNATURE_HEADER = "x-nitt-signature"


class SandboxProvider:
    name = "sandbox"

    def __init__(self, db: Session):
        self.db = db

    def initialize(self, *, reference: str, amount_kobo: int, email: str, metadata: dict) -> dict:
        self.db.add(SandboxCharge(reference=reference))
        self.db.flush()
        return {"authorizationUrl": f"/pay/{reference}"}

    def verify(self, reference: str) -> dict:
        c = self.db.get(SandboxCharge, reference)
        if not c:
            return {"status": "pending", "channel": None, "feeKobo": 0, "paidAt": None, "message": None, "raw": {}}
        status = "success" if c.status in ("success", "refunded") else c.status
        return {"status": status, "channel": c.channel, "feeKobo": c.fee_kobo, "paidAt": c.paid_at,
                "message": c.gateway_response, "raw": {"status": c.status}}

    def refund(self, reference: str) -> dict:
        from ..webhooks import emit_sandbox
        c = self.db.get(SandboxCharge, reference)
        if c:
            c.status = "refunded"
        payment = self.db.scalar(select(Payment).where(Payment.reference == reference))
        emit_sandbox(self.db, payment, "refund.processed", c)
        return {"status": "refunded"}

    def parse_webhook(self, raw_body: bytes, headers: dict) -> dict:
        ok = signature_ok(raw_body, lower_headers(headers).get(SIGNATURE_HEADER), get_config().webhook_signing_key)
        return parse_json_event(raw_body, ok)

    # ---- sandbox-only: what the hosted pay page and the dashboard drive ----
    def settle(self, payment: Payment, status: str, *, channel: str | None = None, message: str | None = None,
               emit: bool = True) -> SandboxCharge:
        """Record the charge outcome on the provider side and (by default) announce it with a signed webhook."""
        from ..payments import compute_fee
        c = self.db.get(SandboxCharge, payment.reference) or SandboxCharge(reference=payment.reference)
        self.db.add(c)
        c.status, c.channel, c.gateway_response, c.awaiting_otp = status, channel or c.channel, message, False
        if status == "success":
            c.fee_kobo, c.paid_at = compute_fee(self.db, payment.amount_kobo), utcnow()
        self.db.flush()
        if emit:
            from ..webhooks import emit_sandbox
            emit_sandbox(self.db, payment, "charge.success" if status == "success" else "charge.failed", c)
        return c

    def reset(self, payment: Payment) -> None:
        """A retry on the same reference starts a fresh attempt."""
        c = self.db.get(SandboxCharge, payment.reference)
        if c:
            # card_attempts is deliberately kept: the 5-attempts limit is per payment reference, not per retry
            c.status, c.channel, c.gateway_response, c.otp_attempts, c.awaiting_otp = "pending", None, None, 0, False
