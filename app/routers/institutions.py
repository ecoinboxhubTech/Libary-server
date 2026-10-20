"""Institution registration (public) and review/management (staff)."""
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import INSTITUTION_STATUSES, INSTITUTION_TYPES, Institution, Member
from ..security import any_staff, client_ip, require_roles
from ..serializers import member_out
from ..services.audit import audit
from ..services.institutions import (StudentsIn, enrol_students, hash_key, institution_out, new_key,
                                     student_count)
from ..timeutil import utcnow

router = APIRouter(prefix="/institutions", tags=["institutions"])
REVIEWERS = require_roles("circulation", "head_librarian", "admin")

_attempts: dict[str, deque] = defaultdict(deque)
REGISTER_LIMIT_PER_HOUR = 5


class RegisterIn(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    type: str = "college"
    email: str = Field(min_length=5, max_length=150)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=300)
    contact_name: str = Field(min_length=2, max_length=150)
    website: str | None = Field(default=None, max_length=200)


class NoteIn(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


class ApproveIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)
    max_students: int | None = Field(default=None, ge=1, le=100000)


def _get(db: Session, institution_id: int) -> Institution:
    inst = db.get(Institution, institution_id)
    if not inst:
        raise HTTPException(404, "Institution not found.")
    return inst


def _review(inst: Institution, actor: Member, status: str, note: str | None) -> dict:
    before = {"status": inst.status}
    inst.status, inst.status_note = status, note
    inst.reviewed_by_id, inst.reviewed_at = actor.id, utcnow()
    return before


# ------------------------------------------------------------------ public
@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """Public. Creates a pending registration and returns a sandbox API key (shown once)."""
    ip, now = client_ip(request) or "?", time.time()
    q = _attempts[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= REGISTER_LIMIT_PER_HOUR:
        raise HTTPException(429, "Too many registrations from this address. Please try again later.")
    q.append(now)
    if body.type not in INSTITUTION_TYPES:
        raise HTTPException(422, f"type must be one of {', '.join(INSTITUTION_TYPES)}")
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "A valid email address is required.")
    if db.scalar(select(Institution.id).where(func.lower(Institution.email) == email,
                                              Institution.status != "rejected")):
        raise HTTPException(409, "An institution with this email is already registered.")
    sandbox_key = new_key(live=False)
    inst = Institution(code="P" + secrets.token_hex(8), name=body.name.strip(), type=body.type, email=email,
                       phone=body.phone, address=body.address, contact_name=body.contact_name.strip(),
                       website=body.website, sandbox_key_hash=hash_key(sandbox_key))
    db.add(inst)
    db.flush()
    inst.code = f"INST{inst.id:04d}"
    audit(db, None, "institution.register", "institution", inst.id, actor_label=f"inst:{inst.code}",
          after={"name": inst.name, "email": inst.email})
    db.commit()
    return {"code": inst.code, "status": inst.status, "sandbox_api_key": sandbox_key,
            "message": "Registration received. Staff will review it. Keep your sandbox key: it is shown only once."}


@router.get("/register/status")
def registration_status(code: str, email: str, db: Session = Depends(get_db)):
    """Public. Lets an applicant check their review result with the code and email they registered with."""
    inst = db.scalar(select(Institution).where(Institution.code == code.strip().upper(),
                                               func.lower(Institution.email) == email.strip().lower()))
    if not inst:
        raise HTTPException(404, "No registration found for that code and email.")
    return {"code": inst.code, "name": inst.name, "status": inst.status, "status_note": inst.status_note}


# ------------------------------------------------------------------ staff
@router.get("")
def list_institutions(status: str | None = None, q: str = "", page: int = Query(1, ge=1),
                      page_size: int = Query(25, ge=1, le=100), _: Member = Depends(any_staff),
                      db: Session = Depends(get_db)):
    stmt = select(Institution)
    if status:
        if status not in INSTITUTION_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(INSTITUTION_STATUSES)}")
        stmt = stmt.where(Institution.status == status)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Institution.name.ilike(like) | Institution.code.ilike(like) | Institution.email.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Institution.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    counts = dict(db.execute(select(Member.institution_id, func.count()).where(
        Member.institution_id.in_([r.id for r in rows] or [0])).group_by(Member.institution_id)).all())
    return {"total": total, "page": page, "page_size": page_size,
            "results": [institution_out(r, counts.get(r.id, 0)) for r in rows]}


@router.get("/{institution_id}")
def get_institution(institution_id: int, _: Member = Depends(any_staff), db: Session = Depends(get_db)):
    inst = _get(db, institution_id)
    return institution_out(inst, student_count(db, inst.id))


@router.post("/{institution_id}/approve")
def approve(institution_id: int, body: ApproveIn, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    """Approves a pending (or previously rejected) institution and issues its live API key. The key is shown once."""
    inst = _get(db, institution_id)
    if inst.status not in ("pending", "rejected"):
        raise HTTPException(409, f"Only pending or rejected institutions can be approved (this one is {inst.status}).")
    key = new_key(live=True)
    before = _review(inst, actor, "approved", body.note)
    inst.live_key_hash, inst.live_key_prefix = hash_key(key), key[:14]
    if body.max_students:
        inst.max_students = body.max_students
    audit(db, actor, "institution.approve", "institution", inst.id, before=before, after={"status": "approved"})
    db.commit()
    return {"institution": institution_out(inst, student_count(db, inst.id)), "live_api_key": key,
            "notice": "Copy the live API key now and send it securely to the institution. It cannot be shown again."}


@router.post("/{institution_id}/reject")
def reject(institution_id: int, body: NoteIn, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    inst = _get(db, institution_id)
    if inst.status != "pending":
        raise HTTPException(409, f"Only pending institutions can be rejected (this one is {inst.status}).")
    before = _review(inst, actor, "rejected", body.reason)
    audit(db, actor, "institution.reject", "institution", inst.id, before=before, after={"reason": body.reason})
    db.commit()
    return institution_out(inst, student_count(db, inst.id))


@router.post("/{institution_id}/suspend")
def suspend(institution_id: int, body: NoteIn, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    """Blocks the live API and stops the institution's students from borrowing until reinstated."""
    inst = _get(db, institution_id)
    if inst.status != "approved":
        raise HTTPException(409, "Only approved institutions can be suspended.")
    before = _review(inst, actor, "suspended", body.reason)
    audit(db, actor, "institution.suspend", "institution", inst.id, before=before, after={"reason": body.reason})
    db.commit()
    return institution_out(inst, student_count(db, inst.id))


@router.post("/{institution_id}/reinstate")
def reinstate(institution_id: int, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    inst = _get(db, institution_id)
    if inst.status != "suspended":
        raise HTTPException(409, "Only suspended institutions can be reinstated.")
    before = _review(inst, actor, "approved", None)
    audit(db, actor, "institution.reinstate", "institution", inst.id, before=before, after={"status": "approved"})
    db.commit()
    return institution_out(inst, student_count(db, inst.id))


@router.post("/{institution_id}/rotate-key")
def rotate_key(institution_id: int, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    """Replaces the live API key (the old one stops working at once). The new key is shown once."""
    inst = _get(db, institution_id)
    if inst.status != "approved":
        raise HTTPException(409, "Keys can only be rotated for approved institutions.")
    key = new_key(live=True)
    inst.live_key_hash, inst.live_key_prefix = hash_key(key), key[:14]
    audit(db, actor, "institution.rotate_key", "institution", inst.id)
    db.commit()
    return {"live_api_key": key, "live_key_prefix": inst.live_key_prefix,
            "notice": "Copy the key now. It cannot be shown again."}


@router.get("/{institution_id}/students")
def institution_students(institution_id: int, q: str = "", page: int = Query(1, ge=1),
                         page_size: int = Query(25, ge=1, le=100), _: Member = Depends(any_staff),
                         db: Session = Depends(get_db)):
    inst = _get(db, institution_id)
    stmt = select(Member).where(Member.institution_id == inst.id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Member.name.ilike(like) | Member.id_number.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Member.name).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size, "results": [member_out(m) for m in rows]}


@router.post("/{institution_id}/students")
def enrol(institution_id: int, body: StudentsIn, actor: Member = Depends(REVIEWERS), db: Session = Depends(get_db)):
    """Staff can enrol students on behalf of an approved institution (same rules as the partner API)."""
    inst = _get(db, institution_id)
    if inst.status != "approved":
        raise HTTPException(409, "Students can only be enrolled for approved institutions.")
    result = enrol_students(db, inst, body.students, actor, actor.id_number)
    db.commit()
    return result
