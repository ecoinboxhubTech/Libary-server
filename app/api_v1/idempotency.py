"""Idempotency-Key support: a repeated request with the same key replays the first response."""
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..models import IdempotencyKey, Member

HEADER = "Idempotency-Key"


def replay(db: Session, member: Member, key: str | None, route: str) -> JSONResponse | None:
    if not key:
        return None
    row = db.get(IdempotencyKey, (key, member.id))
    if not row:
        return None
    if row.route != route:  # the same key must not be reused for a different operation
        from ..apierrors import ApiError
        raise ApiError(409, "CONFLICT", "This Idempotency-Key was already used for a different request.")
    return JSONResponse(row.response, status_code=row.status_code, headers={"Idempotent-Replay": "true"})


def remember(db: Session, member: Member, key: str | None, route: str, status_code: int, response: dict) -> None:
    if key:
        db.add(IdempotencyKey(key=key, member_id=member.id, route=route, status_code=status_code, response=response))
