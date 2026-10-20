"""camelCase JSON shapes for /api/v1. Public ids are opaque strings; money is integer kobo."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Copy, Ebook, Hold, Loan, Title
from ..serializers import availability_map, iso
from ..services import circulation as circ
from ..timeutil import wat_date
from .errors import ApiError, not_found


def kobo(naira: int) -> int:
    return int(naira) * 100


def loan_ref(loan_id: int) -> str:
    return f"ln_{loan_id}"


def hold_ref(hold_id: int) -> str:
    return f"hd_{hold_id}"


def parse_ref(value: str, prefix: str) -> int:
    raw = value[len(prefix) + 1:] if value.startswith(prefix + "_") else value
    if not raw.isdigit():
        raise not_found()
    return int(raw)


def find_title(db: Session, ident: str, *, include_retired: bool = False) -> Title:
    t = db.scalar(select(Title).where(Title.slug == ident))
    if not t and ident.isdigit():
        t = db.get(Title, int(ident))
    if not t or (t.retired and not include_retired):
        raise not_found("Title not found.")
    return t


def title_availability(avail: dict, t: Title) -> dict:
    return {"total": avail["total_copies"], "available": avail["available_copies"], "isDigital": bool(t.is_digital)}


def ebook_pages(db: Session, title_ids: list[int]) -> dict[int, int]:
    """title id -> page count, for the titles that have an e-copy."""
    if not title_ids:
        return {}
    return dict(db.execute(select(Ebook.title_id, Ebook.page_count).where(Ebook.title_id.in_(title_ids))).all())


def title_card(t: Title, avail: dict, pages: int = 0) -> dict:
    return {
        "id": t.slug, "title": t.title, "author": t.authors, "year": t.year, "publisher": t.publisher, "isbn": t.isbn,
        "callNo": t.call_number, "subject": (t.subject_area or "other").capitalize(), "department": t.department,
        "format": t.format, "language": t.language, "blurb": t.description, "topics": t.topics or [],
        "isDigital": bool(t.is_digital), "availability": title_availability(avail, t),
        "hasEbook": pages > 0, "ebookPages": pages,
    }


def title_detail(db: Session, t: Title) -> dict:
    out = title_card(t, availability_map(db, [t.id])[t.id], ebook_pages(db, [t.id]).get(t.id, 0))
    copies = db.scalars(select(Copy).where(Copy.title_id == t.id, Copy.status.notin_(["withdrawn"])).order_by(Copy.id))
    out.update({
        "pages": t.pages, "quote": t.quote, "course": t.course, "lecturer": t.lecturer,
        "repositoryPath": t.repository_path if t.is_digital else None,
        # no barcodes here: members do not need them, and the desk API keeps them for scanning
        "copies": [{"id": f"cp_{c.id}", "status": c.status, "location": c.shelf_location} for c in copies],
    })
    return out


def loan_card(db: Session, loan: Loan, now: datetime, cfg: dict) -> dict:
    overdue = circ.days_overdue(loan, now)
    can_renew, why_not = True, None
    try:
        circ.check_renewable(db, loan, now, cfg)
    except circ.RuleError as e:
        can_renew, why_not = False, e.message
    slug = loan.copy.title.slug
    has_ebook = loan.returned_at is None and loan.copy.title_id in ebook_pages(db, [loan.copy.title_id])
    return {
        "id": loan_ref(loan.id), "titleId": slug, "title": loan.copy.title.title,
        "author": loan.copy.title.authors, "issuedAt": iso(loan.issued_at),
        "dueAt": iso(loan.due_at), "returnedAt": iso(loan.returned_at),
        "daysLeft": max(0, (wat_date(loan.due_at) - wat_date(now)).days), "overdueDays": overdue,
        "renewals": loan.renewals, "canRenew": can_renew, "renewBlockedReason": why_not,
        "collectionPoint": loan.collection_point,
        "canRead": has_ebook, "readUrl": f"/api/v1/titles/{slug}/ebook" if has_ebook else None,
    }


HOLD_STATUS = {"queued": "waiting", "ready": "ready", "fulfilled": "collected", "expired": "expired",
               "cancelled": "cancelled"}


def hold_card(db: Session, h: Hold) -> dict:
    return {
        "id": hold_ref(h.id), "titleId": h.title.slug, "title": h.title.title,
        "queuePosition": max(circ.queue_position(db, h), 0) if h.status == "queued" else 0,
        "status": HOLD_STATUS.get(h.status, h.status),
        "readyUntil": iso(h.expires_at) if h.status == "ready" else None,
    }


def require_owner_or_staff(member, owner_id: int) -> None:
    if member.id != owner_id and member.role not in ("circulation", "head_librarian", "admin"):
        raise ApiError(404, "NOT_FOUND", "Not found.")  # 404, not 403: do not reveal other members' records
