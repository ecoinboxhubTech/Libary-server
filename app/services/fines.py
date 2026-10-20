"""What a member owes, read from the ledger (the single source of truth, in naira) and reported in kobo."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CHARGE_KINDS, LedgerEntry, Loan, Member
from . import circulation as circ
from . import settings_store


def to_kobo(naira: int) -> int:
    return int(naira) * 100


def refresh(db: Session, member: Member, now: datetime) -> None:
    """Bring fines on this member's overdue loans up to today, so nothing here depends on the nightly job."""
    cfg = settings_store.get_all(db)
    for loan in db.scalars(select(Loan).where(Loan.member_id == member.id, Loan.returned_at.is_(None),
                                              Loan.due_at < now)):
        circ.upsert_fine(db, loan, cfg, now)
    db.flush()


def summary(db: Session, member: Member, now: datetime) -> dict:
    """Charges with FIFO settlement. A waiver tied to a loan settles that loan's fine first; any other waiver
    credit and then payments settle the oldest charges first."""
    refresh(db, member, now)
    cfg = settings_store.get_all(db)
    entries = list(db.scalars(select(LedgerEntry).where(LedgerEntry.member_id == member.id)
                              .order_by(LedgerEntry.created_at, LedgerEntry.id)))
    charges = [e for e in entries if e.kind in CHARGE_KINDS]
    left = {e.id: e.amount for e in charges}
    waived = {e.id: 0 for e in charges}
    pay_pool = sum(e.amount for e in entries if e.kind == "payment")
    waive_pool = 0
    for w in (e for e in entries if e.kind == "waiver"):
        target = next((c for c in charges if c.kind == "fine" and c.loan_id and c.loan_id == w.loan_id), None)
        used = min(left[target.id], w.amount) if target else 0
        if target:
            left[target.id] -= used
            waived[target.id] += used
        waive_pool += w.amount - used
    for c in charges:  # oldest first: leftover waiver credit, then payments
        used = min(left[c.id], waive_pool)
        left[c.id] -= used
        waived[c.id] += used
        waive_pool -= used
        used = min(left[c.id], pay_pool)
        left[c.id] -= used
        pay_pool -= used

    loans = {l.id: l for l in db.scalars(select(Loan).where(Loan.id.in_([c.loan_id for c in charges if c.loan_id])))}
    items = []
    for c in charges:
        loan = loans.get(c.loan_id)
        status = "open" if left[c.id] else ("waived" if waived[c.id] else "paid")
        items.append({
            "id": f"fn_{c.id}", "loanId": f"ln_{c.loan_id}" if c.loan_id else None,
            "title": loan.copy.title.title if loan else (c.note or c.kind.title()),
            "daysOverdue": circ.days_overdue(loan, now) if loan and c.kind == "fine" else 0,
            "amountKobo": to_kobo(c.amount), "outstandingKobo": to_kobo(left[c.id]), "status": status,
        })
    gross = sum(c.amount for c in charges)
    paid = sum(e.amount for e in entries if e.kind == "payment")
    waived_total = sum(e.amount for e in entries if e.kind == "waiver")
    return {
        "perDayKobo": to_kobo(cfg["fine_per_day"]), "grossKobo": to_kobo(gross), "paidKobo": to_kobo(paid),
        "waivedKobo": to_kobo(waived_total), "outstandingKobo": to_kobo(sum(left.values())), "items": items,
    }
