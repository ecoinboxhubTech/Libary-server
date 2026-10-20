"""Remita stub. Remita notifies by its own callback format and secret scheme; confirm it with the Remita
integration guide before filling this in."""
from sqlalchemy.orm import Session

from ...apierrors import ApiError


class RemitaProvider:
    name = "remita"

    def __init__(self, db: Session):
        self.db = db

    def _todo(self):
        raise ApiError(501, "PROVIDER_NOT_IMPLEMENTED", "The Remita provider is not wired up yet.")

    def initialize(self, *, reference, amount_kobo, email, metadata):
        self._todo()

    def verify(self, reference):
        self._todo()

    def refund(self, reference):
        self._todo()

    def parse_webhook(self, raw_body: bytes, headers: dict) -> dict:
        return {"valid": False, "event": None, "reference": None, "event_id": None, "data": {}}
