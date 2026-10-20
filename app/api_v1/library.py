"""Catalogue, loans and holds for the storefront (brief sections 6.1, 6.3, 6.4)."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SUBJECT_AREAS, Hold, Loan, Member, Title
from ..security import current_member, optional_member, require_roles
from ..serializers import availability_map
from ..services import circulation as circ
from ..services import search as search_service
from ..services import settings_store
from ..timeutil import utcnow
from . import views
from .errors import ApiError, from_rule, not_found, paged

router = APIRouter()
LIBRARIAN = require_roles("circulation", "head_librarian")


# ------------------------------------------------------------------ 6.1 catalogue
@router.get("/titles", tags=["catalogue"])
def list_titles(q: str | None = None, subject: str | None = None, format: str | None = None,
                available: int = Query(0, ge=0, le=1), sort: str | None = None,
                page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=50),
                member: Member | None = Depends(optional_member), db: Session = Depends(get_db)):
    if sort and sort not in search_service.SORTS and sort != "relevance":
        raise ApiError(422, "VALIDATION_FAILED", "Some fields are invalid.",
                       {"sort": f"Use one of: relevance, {', '.join(search_service.SORTS)}"})
    filters = {"format": format, "available_only": bool(available)}
    if subject:
        key = subject.strip().lower()
        filters["subject_area" if key in SUBJECT_AREAS else "department"] = key if key in SUBJECT_AREAS else subject
    res = search_service.search_titles(db, q, filters, page, limit, member.id if member else None, sort=sort)
    titles = {t.id: t for t in db.scalars(select(Title).where(Title.id.in_([r["id"] for r in res["results"]])))}
    avail, pages = availability_map(db, list(titles)), views.ebook_pages(db, list(titles))
    data = [views.title_card(titles[r["id"]], avail[r["id"]], pages.get(r["id"], 0))
            for r in res["results"] if r["id"] in titles]
    return paged(data, page, limit, res["total"])


@router.get("/titles/{title_id}", tags=["catalogue"])
def get_title(title_id: str, db: Session = Depends(get_db)):
    return views.title_detail(db, views.find_title(db, title_id))


@router.get("/titles/{title_id}/related", tags=["catalogue"])
def related_titles(title_id: str, limit: int = Query(6, ge=1, le=20), db: Session = Depends(get_db)):
    """Titles that share topics, department or subject area. Feeds 'Borrow these together' and 'More like this'."""
    t = views.find_title(db, title_id)
    mine = {x.lower() for x in (t.topics or [])}
    pool = db.scalars(select(Title).where(Title.retired.is_(False), Title.id != t.id,
                                          (Title.subject_area == t.subject_area) | (Title.department == t.department)
                                          if t.department else Title.subject_area == t.subject_area)).all()

    def score(o: Title) -> int:
        return (3 * len(mine & {x.lower() for x in (o.topics or [])}) + (2 if o.department == t.department else 0)
                + (1 if o.subject_area == t.subject_area else 0))

    ranked = sorted(pool, key=lambda o: (-score(o), o.title.lower()))[:limit]
    avail, pages = availability_map(db, [o.id for o in ranked]), views.ebook_pages(db, [o.id for o in ranked])
    return {"data": [views.title_card(o, avail[o.id], pages.get(o.id, 0)) for o in ranked]}


# ------------------------------------------------------------------ 6.3 loans
def _loan_for(db: Session, ref: str) -> Loan:
    loan = db.get(Loan, views.parse_ref(ref, "ln"))
    if not loan:
        raise not_found("Loan not found.")
    return loan


@router.get("/me/loans", tags=["loans"])
def my_loans(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
             actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    now, cfg = utcnow(), settings_store.get_all(db)
    stmt = select(Loan).where(Loan.member_id == actor.id, Loan.returned_at.is_(None))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Loan.due_at).offset((page - 1) * limit).limit(limit)).all()
    return paged([views.loan_card(db, l, now, cfg) for l in rows], page, limit, total)


@router.post("/loans/{loan_id}/renew", tags=["loans"])
def renew_loan(loan_id: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    loan = _loan_for(db, loan_id)
    views.require_owner_or_staff(actor, loan.member_id)
    if actor.id != loan.member_id and actor.role not in ("circulation", "head_librarian"):
        raise ApiError(403, "FORBIDDEN", "You do not have permission to do this.")
    try:
        circ.renew(db, loan, actor)
    except circ.RuleError as e:
        raise from_rule(e) from None
    db.commit()
    return views.loan_card(db, loan, utcnow(), settings_store.get_all(db))


class ReturnIn(BaseModel):
    condition: str = Field(default="ok", pattern="^(ok|damaged)$")


@router.post("/loans/{loan_id}/return", tags=["loans"])
def return_loan(loan_id: str, body: ReturnIn | None = None, officer: Member = Depends(LIBRARIAN),
                db: Session = Depends(get_db)):
    loan = _loan_for(db, loan_id)
    now = utcnow()
    try:
        res = circ.return_copy(db, loan.copy, officer, condition=(body.condition if body else "ok"), now=now)
    except circ.RuleError as e:
        raise from_rule(e) from None
    db.commit()
    hold = res["hold"]
    return {
        "loan": views.loan_card(db, loan, now, settings_store.get_all(db)),
        "fineKobo": views.kobo(res["fine"]), "copyStatus": res["copy_status"],
        "holdReadyFor": hold.member.id_number if hold else None,
    }


# ------------------------------------------------------------------ 6.4 holds
class HoldIn(BaseModel):
    titleId: str = Field(min_length=1, max_length=120)


@router.post("/holds", status_code=201, tags=["holds"])
def place_hold(body: HoldIn, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    title = views.find_title(db, body.titleId)
    try:
        hold = circ.place_hold(db, actor, title)
    except circ.RuleError as e:
        raise from_rule(e) from None
    db.commit()
    return views.hold_card(db, hold)


@router.get("/me/holds", tags=["holds"])
def my_holds(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
             actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    stmt = select(Hold).where(Hold.member_id == actor.id, Hold.status.in_(["queued", "ready"]))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Hold.queued_at).offset((page - 1) * limit).limit(limit)).all()
    return paged([views.hold_card(db, h) for h in rows], page, limit, total)


@router.delete("/holds/{hold_id}", tags=["holds"])
def cancel_hold(hold_id: str, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    hold = db.get(Hold, views.parse_ref(hold_id, "hd"))
    if not hold:
        raise not_found("Hold not found.")
    views.require_owner_or_staff(actor, hold.member_id)
    if actor.id != hold.member_id and actor.role not in ("circulation", "head_librarian"):
        raise ApiError(403, "FORBIDDEN", "You do not have permission to do this.")
    try:
        circ.cancel_hold(db, hold, actor)
    except circ.RuleError as e:
        raise from_rule(e) from None
    db.commit()
    return views.hold_card(db, hold)
