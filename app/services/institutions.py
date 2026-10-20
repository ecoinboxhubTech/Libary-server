"""Partner institutions: API keys, student enrolment and serialisation."""
import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Institution, Member
from ..security import hash_password
from .audit import audit


# ------------------------------------------------------------------ API keys
def new_key(live: bool) -> str:
    return f"nitt_{'live' if live else 'test'}_{secrets.token_urlsafe(32)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _institution_for_key(db: Session, key: str | None, live: bool) -> Institution:
    if not key:
        raise HTTPException(401, "Missing API key. Send it in the X-API-Key header.")
    col = Institution.live_key_hash if live else Institution.sandbox_key_hash
    inst = db.scalar(select(Institution).where(col == hash_key(key.strip())))
    if not inst:
        raise HTTPException(401, "Invalid API key.")
    return inst


def live_institution(x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> Institution:
    inst = _institution_for_key(db, x_api_key, live=True)
    if inst.status != "approved":
        raise HTTPException(403, f"This institution is {inst.status}. The live API is available to approved institutions only.")
    return inst


def sandbox_institution(x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> Institution:
    inst = _institution_for_key(db, x_api_key, live=False)
    if inst.status == "rejected":
        raise HTTPException(403, "This registration was rejected. The sandbox is no longer available.")
    return inst


# ------------------------------------------------------------------ serialisation
def student_count(db: Session, institution_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Member).where(Member.institution_id == institution_id)) or 0


def institution_out(inst: Institution, students: int | None = None) -> dict:
    from ..serializers import iso
    return {
        "id": inst.id, "code": inst.code, "name": inst.name, "type": inst.type, "email": inst.email,
        "phone": inst.phone, "address": inst.address, "contact_name": inst.contact_name, "website": inst.website,
        "status": inst.status, "status_note": inst.status_note, "max_students": inst.max_students,
        "student_count": students, "has_live_key": inst.live_key_hash is not None,
        "live_key_prefix": inst.live_key_prefix, "reviewed_at": iso(inst.reviewed_at), "created_at": iso(inst.created_at),
    }


# ------------------------------------------------------------------ student enrolment
class StudentIn(BaseModel):
    id_number: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=2, max_length=150)
    programme: str | None = None
    level: str | None = None
    department: str | None = None
    phone: str | None = None
    email: str | None = None
    temporary_password: str | None = Field(default=None, min_length=8, max_length=100)


class StudentsIn(BaseModel):
    students: list[StudentIn] = Field(min_length=1, max_length=200)


FIELDS = ("name", "programme", "level", "department", "phone", "email")


def enrol_students(db: Session, inst: Institution, rows: list[StudentIn], actor: Member | None,
                   actor_label: str) -> dict:
    """Creates or updates students of `inst`. One bad row never blocks the others."""
    created = updated = 0
    errors: list[dict] = []
    results: list[dict] = []
    seen: set[str] = set()
    room = inst.max_students - student_count(db, inst.id)
    for i, row in enumerate(rows):
        idn = row.id_number.strip().upper()

        def fail(msg: str):
            errors.append({"index": i, "id_number": idn, "error": msg})
            results.append({"id_number": idn, "status": "error"})

        if idn in seen:
            fail("duplicate id_number inside this request")
            continue
        seen.add(idn)
        if row.email and "@" not in row.email:
            fail("email is not valid")
            continue
        existing = db.scalar(select(Member).where(Member.id_number == idn))
        if existing:
            if existing.institution_id != inst.id:
                fail("this ID number already belongs to another account")
                continue
            for f in FIELDS:
                v = getattr(row, f)
                if v is not None:
                    setattr(existing, f, v.strip() or None)
            updated += 1
            results.append({"id_number": idn, "status": "updated"})
            continue
        if room <= 0:
            fail(f"student quota reached ({inst.max_students})")
            continue
        m = Member(id_number=idn, name=row.name.strip(), member_type="student", role="student",
                   programme=row.programme, level=row.level, department=row.department, phone=row.phone,
                   email=row.email, institution_id=inst.id, must_change_password=bool(row.temporary_password))
        if row.temporary_password:
            m.password_hash = hash_password(row.temporary_password)
        db.add(m)
        room -= 1
        created += 1
        results.append({"id_number": idn, "status": "created"})
    db.flush()
    audit(db, actor, "institution.enrol", "institution", inst.id, actor_label=actor_label,
          after={"created": created, "updated": updated, "errors": len(errors)})
    return {"created": created, "updated": updated, "errors": errors, "results": results}
