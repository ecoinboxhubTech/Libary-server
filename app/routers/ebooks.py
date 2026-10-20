"""E-copies of titles. Admins/head librarians upload them; members read them page by page while the title is on loan.
Pages are served as JSON text with no-store caching. The original upload is never stored, so nothing is downloadable."""
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import STAFF_ROLES, Copy, Ebook, Loan, Member, Title
from ..security import current_member, require_roles
from ..services import ebooks as ebook_service
from ..services.audit import audit
from ..timeutil import utcnow

router = APIRouter(prefix="/ebooks", tags=["ebooks"])
MANAGER = require_roles("admin", "head_librarian")
MAX_UPLOAD = 2 * 1024 * 1024  # text only, so 2 MB is generous


def _title(db: Session, title_id: int) -> Title:
    t = db.get(Title, title_id)
    if not t or t.retired:
        raise HTTPException(404, "Title not found.")
    return t


def _ebook(db: Session, title_id: int) -> Ebook | None:
    return db.scalar(select(Ebook).where(Ebook.title_id == title_id))


def can_read(db: Session, member: Member, title_id: int) -> bool:
    """Staff may read any e-copy; everyone else needs the title on an open loan."""
    if member.role in STAFF_ROLES:
        return True
    return db.scalar(select(Loan.id).join(Copy, Copy.id == Loan.copy_id)
                     .where(Loan.member_id == member.id, Loan.returned_at.is_(None), Copy.title_id == title_id)
                     .limit(1)) is not None


def _info(db: Session, member: Member, title_id: int) -> dict:
    book = _ebook(db, title_id)
    if not book:
        return {"title_id": title_id, "available": False, "page_count": 0, "can_read": False}
    ok = can_read(db, member, title_id)
    return {"title_id": title_id, "available": True, "page_count": book.page_count, "can_read": ok,
            "reason": None if ok else "Borrow this title to read its e-book.",
            "updated_at": book.updated_at.isoformat() + "Z"}


@router.get("/{title_id}")
def ebook_info(title_id: int, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    """Whether the title has an e-book and whether the signed-in member can read it right now."""
    _title(db, title_id)
    return _info(db, member, title_id)


@router.get("/{title_id}/pages/{page}")
def read_page(title_id: int, page: int, response: Response, member: Member = Depends(current_member),
              db: Session = Depends(get_db)):
    t = _title(db, title_id)
    book = _ebook(db, title_id)
    if not book:
        raise HTTPException(404, "This title has no e-book.")
    if not can_read(db, member, title_id):
        raise HTTPException(403, "Borrow this title to read its e-book.")
    if page < 1 or page > book.page_count:
        raise HTTPException(404, "That page does not exist.")
    response.headers["Cache-Control"] = "no-store"
    return {"title_id": t.id, "title": t.title, "page": page, "page_count": book.page_count, "text": book.pages[page - 1]}


def _save(db: Session, member: Member, t: Title, pages: list[str], filename: str, action: str) -> dict:
    if not pages:
        raise HTTPException(400, "The file has no readable text.")
    if len(pages) > ebook_service.MAX_PAGES:
        raise HTTPException(400, f"An e-book can have at most {ebook_service.MAX_PAGES} pages.")
    book = _ebook(db, t.id)
    before = {"page_count": book.page_count} if book else None
    if book:
        book.pages, book.page_count, book.filename, book.uploaded_by_id, book.updated_at = pages, len(pages), filename, member.id, utcnow()
    else:
        db.add(Ebook(title_id=t.id, pages=pages, page_count=len(pages), filename=filename, uploaded_by_id=member.id))
    audit(db, member, action, "ebook", t.id, before=before, after={"page_count": len(pages), "filename": filename})
    db.commit()
    return _info(db, member, t.id)


@router.post("/{title_id}")
async def upload(title_id: int, file: UploadFile = File(...), member: Member = Depends(MANAGER), db: Session = Depends(get_db)):
    """Upload a .txt or .md file. Separate pages with a line containing only `---` (or a form feed);
    without either the text is split into pages automatically. Replaces any existing e-book for the title."""
    t = _title(db, title_id)
    name = (file.filename or "ebook.txt")[:200]
    if not name.lower().endswith((".txt", ".md")):
        raise HTTPException(400, "Upload a .txt or .md file.")
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "That file is larger than 2 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "The file must be UTF-8 text.")
    return _save(db, member, t, ebook_service.paginate(text), name, "ebook.upload")


@router.post("/{title_id}/sample")
def make_sample(title_id: int, member: Member = Depends(MANAGER), db: Session = Depends(get_db)):
    """Create a short placeholder e-book so the reader can be tried before real content is uploaded."""
    t = _title(db, title_id)
    return _save(db, member, t, ebook_service.sample_pages(t.title, t.authors, t.subject_area), "sample.txt", "ebook.sample")


@router.delete("/{title_id}")
def remove(title_id: int, member: Member = Depends(MANAGER), db: Session = Depends(get_db)):
    _title(db, title_id)
    book = _ebook(db, title_id)
    if not book:
        raise HTTPException(404, "This title has no e-book.")
    audit(db, member, "ebook.delete", "ebook", title_id, before={"page_count": book.page_count})
    db.delete(book)
    db.commit()
    return {"ok": True}
