"""Paystack stub. Webhook signature checking is real (HMAC-SHA512 of the raw body with the secret key);
initialize/verify/refund still need the HTTP calls wired in once the merchant account exists."""
from sqlalchemy.orm import Session

from ...apierrors import ApiError
from ...config import get_config
from . import lower_headers, parse_json_event, signature_ok


class PaystackProvider:
    name = "paystack"

    def __init__(self, db: Session):
        self.db = db

    def _todo(self):
        raise ApiError(501, "PROVIDER_NOT_IMPLEMENTED", "The Paystack provider is not wired up yet.")

    def initialize(self, *, reference, amount_kobo, email, metadata):
        self._todo()

    def verify(self, reference):
        self._todo()

    def refund(self, reference):
        self._todo()

    def parse_webhook(self, raw_body: bytes, headers: dict) -> dict:
        ok = signature_ok(raw_body, lower_headers(headers).get("x-paystack-signature"),
                          get_config().payment_secret_key)
        return parse_json_event(raw_body, ok)
