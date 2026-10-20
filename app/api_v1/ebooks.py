"""E-copies under /api/v1 (addressed by title slug). This is a thin layer over the logic in routers/ebooks.py, so the
rules live in one place: staff manage e-copies, members read them only while they have the title on loan, and
there is no endpoint that returns a file, so there is nothing to download."""
from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Member
from ..routers import ebooks as legacy
from ..security import current_member
from . import views

router = APIRouter(tags=["ebooks"])
STAFF = legacy.MANAGER


def _info(slug: str, raw: dict) -> dict:
    return {"titleId": slug, "available": raw["available"], "pageCount": raw["page_count"], "canRead": raw["can_read"],
            "reason": raw.get("reason"), "updatedAt": raw.get("updated_at"),
            "restrictions": {"download": False, "print": False}}


@router.get("/titles/{title_id}/ebook")
def ebook_info(title_id: str, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    """Does this title have an e-copy, and can the signed-in member read it right now?"""
    t = views.find_title(db, title_id)
    return _info(t.slug, legacy.ebook_info(t.id, member, db))


@router.get("/titles/{title_id}/ebook/pages/{page}")
def read_page(title_id: str, page: int, response: Response, member: Member = Depends(current_member),
              db: Session = Depends(get_db)):
    t = views.find_title(db, title_id)
    raw = legacy.read_page(t.id, page, response, member, db)  # 403 unless on loan (or staff), 404 past the end
    response.headers["X-Content-Type-Options"] = "nosniff"
    return {"titleId": t.slug, "title": raw["title"], "page": raw["page"], "pageCount": raw["page_count"],
            "text": raw["text"]}


@router.post("/titles/{title_id}/ebook")
async def upload_ebook(title_id: str, file: UploadFile = File(...), member: Member = Depends(STAFF),
                       db: Session = Depends(get_db)):
    """Admin / head librarian. A .txt or .md file; pages split on a line holding only `---` or sized automatically."""
    t = views.find_title(db, title_id)
    return _info(t.slug, await legacy.upload(t.id, file, member, db))


@router.post("/titles/{title_id}/ebook/sample")
def sample_ebook(title_id: str, member: Member = Depends(STAFF), db: Session = Depends(get_db)):
    """A four-page placeholder e-book, so the reader can be tried before real content is uploaded."""
    t = views.find_title(db, title_id)
    return _info(t.slug, legacy.make_sample(t.id, member, db))


@router.delete("/titles/{title_id}/ebook")
def delete_ebook(title_id: str, member: Member = Depends(STAFF), db: Session = Depends(get_db)):
    t = views.find_title(db, title_id)
    return legacy.remove(t.id, member, db)
