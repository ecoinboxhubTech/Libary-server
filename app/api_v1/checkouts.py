"""Checkout (brief section 6.2). The basket lives in the browser; this is called once, on confirm."""
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Checkout, Loan, Member, Title
from ..security import current_member
from ..serializers import iso
from ..services import checkout as svc
from ..services import payments
from ..timeutil import utcnow
from . import idempotency, views
from .errors import ApiError, not_found

router = APIRouter(tags=["checkout"])
STAFF_VIEW = ("circulation", "head_librarian", "finance", "admin")


class CheckoutIn(BaseModel):
    items: list[str] = Field(min_length=1, max_length=20)
    collectionPoint: str = Field(default="desk", pattern=r"^[a-z0-9_]{1,30}$")
    periodDays: int = 14
    payFines: bool = False
    paymentMethod: str = Field(default="online", pattern="^(online|desk)$")
    # Any amount the browser sends is ignored: the model has no amount field, and the server reads the ledger.


def card(db: Session, c: Checkout) -> dict:
    titles = {t.id: t for t in db.scalars(select(Title).where(Title.id.in_(c.items)))}
    out = {
        "id": c.id, "status": c.status, "finesToPayKobo": c.fines_to_pay_kobo, "collectionPoint": c.collection_point,
        "periodDays": c.period_days, "expiresAt": iso(c.expires_at) if c.status in svc.OPEN_STATUSES else None,
        "items": [{"titleId": titles[i].slug, "title": titles[i].title} for i in c.items if i in titles],
    }
    if c.payment_reference:
        out["payment"] = {"reference": c.payment_reference,
                          "authorizationUrl": payments.authorization_url_for(db, c.payment_reference)}
    if c.status == "confirmed":
        loans = db.scalars(select(Loan).where(Loan.checkout_id == c.id).order_by(Loan.id))
        out["loans"] = [{"id": views.loan_ref(l.id), "title": l.copy.title.title, "titleId": l.copy.title.slug,
                         "dueAt": iso(l.due_at)} for l in loans]
    return out


@router.post("/checkouts", status_code=201)
def create_checkout(body: CheckoutIn, idempotency_key: str | None = Header(default=None, alias=idempotency.HEADER),
                    actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    again = idempotency.replay(db, actor, idempotency_key, "POST /checkouts")
    if again:
        return again
    seen, titles = set(), []
    for ident in body.items:
        t = views.find_title(db, ident)
        if t.id not in seen:
            seen.add(t.id)
            titles.append(t)
    checkout = svc.create(db, actor, titles, collection_point=body.collectionPoint, period_days=body.periodDays,
                          pay_fines=body.payFines, pay_online=body.paymentMethod == "online")
    out = card(db, checkout)
    idempotency.remember(db, actor, idempotency_key, "POST /checkouts", 201, out)
    db.commit()
    return JSONResponse(out, status_code=201)


def _mine(db: Session, actor: Member, checkout_id: str) -> Checkout:
    c = db.get(Checkout, checkout_id)
    if not c or (c.member_id != actor.id and actor.role not in STAFF_VIEW):
        raise not_found("Checkout not found.")
    return c


@router.get("/checkouts/{checkout_id}")
def get_checkout(checkout_id: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    c = _mine(db, actor, checkout_id)
    if svc.expire_stale(db, checkout=c):
        db.commit()
    return card(db, c)


@router.delete("/checkouts/{checkout_id}")
def cancel_checkout(checkout_id: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    c = _mine(db, actor, checkout_id)
    if c.member_id != actor.id:
        raise ApiError(403, "FORBIDDEN", "You do not have permission to do this.")
    svc.cancel(db, c, actor)
    db.commit()
    return card(db, c)
