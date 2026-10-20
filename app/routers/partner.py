"""Partner API for institutions, authenticated with an X-API-Key header.
  /api/partner/v1  live data, approved institutions only (live key)
  /api/sandbox/v1  same shapes, fixed sample data, nothing is ever written (sandbox key)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Institution, Loan, Member, Title
from ..serializers import availability_map, iso, loan_out, member_out, title_out
from ..services import circulation as circ
from ..services import search as search_service
from ..services import settings_store
from ..services.audit import audit
from ..services.institutions import (StudentsIn, enrol_students, institution_out, live_institution,
                                     sandbox_institution, student_count)
from ..timeutil import utcnow

router = APIRouter(prefix="/partner/v1", tags=["partner-api"])
sandbox = APIRouter(prefix="/sandbox/v1", tags=["partner-sandbox"])


class StudentPatch(BaseModel):
    name: str | None = None
    programme: str | None = None
    level: str | None = None
    department: str | None = None
    phone: str | None = None
    email: str | None = None
    status: str | None = None  # active | suspended


def _student(db: Session, inst: Institution, id_number: str) -> Member:
    m = db.scalar(select(Member).where(Member.id_number == id_number.strip().upper(),
                                       Member.institution_id == inst.id))
    if not m:
        raise HTTPException(404, "No student with that ID number at your institution.")
    return m


# ================================================================== LIVE
@router.get("/me")
def me(inst: Institution = Depends(live_institution), db: Session = Depends(get_db)):
    n = student_count(db, inst.id)
    active = db.scalar(select(func.count()).select_from(Loan).join(Member, Member.id == Loan.member_id)
                       .where(Member.institution_id == inst.id, Loan.returned_at.is_(None))) or 0
    return {**institution_out(inst, n), "mode": "live", "active_loans": active, "quota_remaining": inst.max_students - n}


@router.get("/catalogue/search")
def catalogue_search(q: str | None = None, subject_area: str | None = None, department: str | None = None,
                     format: str | None = None, language: str | None = None, year: int | None = None,
                     available_only: bool = False, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50),
                     _: Institution = Depends(live_institution), db: Session = Depends(get_db)):
    filters = {"subject_area": subject_area, "department": department, "format": format, "language": language,
               "year": year, "available_only": available_only}
    return search_service.search_titles(db, q, filters, page, page_size, None)


@router.get("/catalogue/titles/{title_id}")
def catalogue_title(title_id: int, _: Institution = Depends(live_institution), db: Session = Depends(get_db)):
    t = db.get(Title, title_id)
    if not t or t.retired:
        raise HTTPException(404, "Title not found.")
    return title_out(t, availability_map(db, [t.id])[t.id])


@router.post("/students")
def post_students(body: StudentsIn, inst: Institution = Depends(live_institution), db: Session = Depends(get_db)):
    """Create or update up to 200 students per call. Rows are processed independently."""
    result = enrol_students(db, inst, body.students, None, f"inst:{inst.code}")
    db.commit()
    return result


@router.get("/students")
def list_students(q: str = "", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                  inst: Institution = Depends(live_institution), db: Session = Depends(get_db)):
    stmt = select(Member).where(Member.institution_id == inst.id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Member.name.ilike(like) | Member.id_number.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Member.name).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size, "results": [member_out(m) for m in rows]}


@router.get("/students/{id_number:path}/loans")
def student_loans(id_number: str, history: bool = False, inst: Institution = Depends(live_institution),
                  db: Session = Depends(get_db)):
    m = _student(db, inst, id_number)
    now, cfg = utcnow(), settings_store.get_all(db)
    stmt = select(Loan).where(Loan.member_id == m.id).order_by(Loan.issued_at.desc())
    if not history:
        stmt = stmt.where(Loan.returned_at.is_(None))
    return {"student": member_out(m), "fines_owed": max(0, circ.member_balance(db, m.id)),
            "loans": [loan_out(l, now, cfg) for l in db.scalars(stmt.limit(200))]}


@router.patch("/students/{id_number:path}")
def patch_student(id_number: str, body: StudentPatch, inst: Institution = Depends(live_institution),
                  db: Session = Depends(get_db)):
    m = _student(db, inst, id_number)
    data = body.model_dump(exclude_unset=True)
    if "status" in data:
        if data["status"] not in ("active", "suspended"):
            raise HTTPException(422, "status must be 'active' or 'suspended'.")
        if data["status"] == "suspended" and circ.open_loans(db, m.id):
            raise HTTPException(409, "Student still has items on loan. They must be returned first.")
    before = {k: getattr(m, k) for k in data}
    for k, v in data.items():
        setattr(m, k, v)
    audit(db, None, "institution.student_update", "member", m.id, actor_label=f"inst:{inst.code}", before=before, after=data)
    db.commit()
    return member_out(m)


# ================================================================== SANDBOX (stateless, canned data)
SAMPLE_TITLES = [
    {"id": 9001, "title": "Sample: Maritime Economics", "subtitle": None, "authors": "A. Author", "edition": "2nd",
     "publisher": "Sample Press", "year": 2019, "isbn": "9780000000011", "subjects": "Shipping; Economics",
     "subject_area": "maritime", "call_number": "HE571", "language": "English", "department": "Maritime Studies",
     "format": "print_book", "description": "Sandbox record.", "retired": False,
     "total_copies": 3, "available_copies": 2, "locations": ["Section A / Shelf 2"], "holds": 0},
    {"id": 9002, "title": "Sample: Supply Chain Basics", "subtitle": None, "authors": "B. Writer", "edition": None,
     "publisher": "Sample Press", "year": 2021, "isbn": "9780000000028", "subjects": "Logistics",
     "subject_area": "logistics", "call_number": "HD38.5", "language": "English", "department": "Logistics and Supply Chain",
     "format": "print_book", "description": "Sandbox record.", "retired": False,
     "total_copies": 1, "available_copies": 0, "locations": [], "holds": 2},
    {"id": 9003, "title": "Sample: Highway Engineering", "subtitle": None, "authors": "C. Engineer", "edition": "1st",
     "publisher": "Sample Press", "year": 2015, "isbn": "9780000000035", "subjects": "Roads; Traffic",
     "subject_area": "road", "call_number": "TE145", "language": "English", "department": "Transport Management",
     "format": "print_book", "description": "Sandbox record.", "retired": False,
     "total_copies": 2, "available_copies": 2, "locations": ["Section C / Shelf 2"], "holds": 0},
]


def _sample_student(inst: Institution, n: int, name: str) -> dict:
    return {"id": 9000 + n, "id_number": f"SBX/{inst.code}/{n:03d}", "name": name, "member_type": "student",
            "role": "student", "programme": "Sample Programme", "level": "ND I", "department": "Transport Management",
            "phone": None, "email": f"student{n}@sandbox.test", "status": "active", "expires_on": None,
            "must_change_password": False, "consented": True, "activated": True, "institution_id": inst.id}


@sandbox.get("/me")
def sandbox_me(inst: Institution = Depends(sandbox_institution)):
    return {**institution_out(inst, 2), "mode": "sandbox", "active_loans": 1, "quota_remaining": inst.max_students - 2,
            "notice": "Sandbox: sample data only. Nothing here affects real records."}


@sandbox.get("/catalogue/search")
def sandbox_search(q: str | None = None, available_only: bool = False, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=50), _: Institution = Depends(sandbox_institution)):
    needle = (q or "").strip().lower()
    rows = [t for t in SAMPLE_TITLES if (not needle or needle in (t["title"] + " " + (t["subjects"] or "")).lower())
            and (not available_only or t["available_copies"] > 0)]
    return {"total": len(rows), "page": page, "page_size": page_size,
            "results": rows[(page - 1) * page_size: page * page_size]}


@sandbox.get("/catalogue/titles/{title_id}")
def sandbox_title(title_id: int, _: Institution = Depends(sandbox_institution)):
    for t in SAMPLE_TITLES:
        if t["id"] == title_id:
            return t
    raise HTTPException(404, "Title not found. Sandbox titles are 9001, 9002 and 9003.")


@sandbox.post("/students")
def sandbox_post_students(body: StudentsIn, _: Institution = Depends(sandbox_institution)):
    """Validates like the live endpoint but saves nothing.
    Test triggers: an id_number starting with EXISTS reports 'updated'; starting with ERR reports an error."""
    created = updated = 0
    errors, results, seen = [], [], set()
    for i, row in enumerate(body.students):
        idn = row.id_number.strip().upper()
        err = ("duplicate id_number inside this request" if idn in seen else
               "email is not valid" if row.email and "@" not in row.email else
               "Simulated failure (id_number starts with ERR)" if idn.startswith("ERR") else None)
        seen.add(idn)
        if err:
            errors.append({"index": i, "id_number": idn, "error": err})
            results.append({"id_number": idn, "status": "error"})
        elif idn.startswith("EXISTS"):
            updated += 1
            results.append({"id_number": idn, "status": "updated"})
        else:
            created += 1
            results.append({"id_number": idn, "status": "created"})
    return {"created": created, "updated": updated, "errors": errors, "results": results, "sandbox": True}


@sandbox.get("/students")
def sandbox_students(inst: Institution = Depends(sandbox_institution)):
    rows = [_sample_student(inst, 1, "Sandbox Student One"), _sample_student(inst, 2, "Sandbox Student Two")]
    return {"total": 2, "page": 1, "page_size": 25, "results": rows}


@sandbox.get("/students/{id_number:path}/loans")
def sandbox_student_loans(id_number: str, history: bool = False, inst: Institution = Depends(sandbox_institution)):
    now = utcnow()
    if not id_number.upper().startswith(f"SBX/{inst.code}/"):
        raise HTTPException(404, "No sandbox student with that ID. Use the ids from GET /sandbox/v1/students.")
    loans = [{"id": 1, "title": SAMPLE_TITLES[0]["title"], "title_id": 9001, "authors": "A. Author", "barcode": "SBX0000001",
              "collection": "general", "issued_at": iso(now), "due_at": iso(now), "returned_at": None, "renewals": 0,
              "is_overdue": False, "days_overdue": 0,
              "member": {"id": 9001, "id_number": id_number.upper(), "name": "Sandbox Student One"}}]
    if history:
        loans.append({**loans[0], "id": 2, "title": SAMPLE_TITLES[2]["title"], "title_id": 9003, "barcode": "SBX0000002",
                      "returned_at": iso(now)})
    return {"student": _sample_student(inst, 1, "Sandbox Student One"), "fines_owed": 0, "loans": loans}


@sandbox.patch("/students/{id_number:path}")
def sandbox_patch_student(id_number: str, body: StudentPatch, inst: Institution = Depends(sandbox_institution)):
    if body.status and body.status not in ("active", "suspended"):
        raise HTTPException(422, "status must be 'active' or 'suspended'.")
    return {**_sample_student(inst, 1, "Sandbox Student One"), **body.model_dump(exclude_unset=True), "id_number": id_number.upper()}
