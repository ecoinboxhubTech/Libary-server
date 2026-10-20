"""Fines (brief section 6.5)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CHARGE_KINDS, LedgerEntry, Member, Payment
from ..security import current_member, require_roles
from ..services import fines
from ..services.audit import audit
from ..serializers import iso
from ..timeutil import utcnow
from . import views
from .errors import ApiError, not_found

router = APIRouter(tags=["fines"])
HEAD = require_roles("head_librarian")  # FR-6.2: only the Head Librarian may waive


def payment_row(p: Payment) -> dict:
    return {"reference": p.reference, "amountKobo": p.amount_kobo, "status": p.status, "createdAt": iso(p.created_at)}


@router.get("/me/fines")
def my_fines(actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    out = fines.summary(db, actor, utcnow())
    db.commit()  # refresh() may have posted a fine for a loan that went overdue today
    pays = db.scalars(select(Payment).where(Payment.member_id == actor.id).order_by(Payment.created_at.desc()).limit(50))
    out["payments"] = [payment_row(p) for p in pays]
    return out


class WaiveIn(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


@router.post("/fines/{fine_id}/waive")
def waive_fine(fine_id: str, body: WaiveIn, officer: Member = Depends(HEAD), db: Session = Depends(get_db)):
    entry = db.get(LedgerEntry, views.parse_ref(fine_id, "fn"))
    if not entry or entry.kind not in CHARGE_KINDS:
        raise not_found("Fine not found.")
    member = db.get(Member, entry.member_id)
    now = utcnow()
    item = next((i for i in fines.summary(db, member, now)["items"] if i["id"] == f"fn_{entry.id}"), None)
    if not item or item["outstandingKobo"] <= 0:
        raise ApiError(409, "CONFLICT", "This fine has nothing left to waive.")
    amount = item["outstandingKobo"] // 100
    waiver = LedgerEntry(member_id=member.id, loan_id=entry.loan_id, kind="waiver", amount=amount, note=body.reason,
                         officer_id=officer.id)
    db.add(waiver)
    db.flush()
    audit(db, officer, "fine.waiver", "ledger", waiver.id,
          after={"member": member.id_number, "fine": f"fn_{entry.id}", "amount": amount, "reason": body.reason})
    db.commit()
    return {"id": f"fn_{entry.id}", "status": "waived", "waivedKobo": amount * 100,
            "outstandingKobo": fines.summary(db, member, now)["outstandingKobo"]}
