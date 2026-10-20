"""Approval workflows. Members raise requests; staff approve or reject them.
  loan requests    -> approval issues the loan
  return requests  -> approval confirms receipt and closes the loan (fine is calculated then)
  payment requests -> approval (admin / head librarian) writes the ledger payment"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (REQUEST_STATUSES, Copy, Loan, LoanRequest, Member, PaymentRequest, ReturnRequest, Title,
                      LedgerEntry)
from ..security import any_staff, current_member, require_roles
from ..serializers import iso, loan_out
from ..services import circulation as circ
from ..services import settings_store
from ..services.audit import audit
from ..services.notifications import notify
from ..timeutil import utcnow
from .circulation import _return_payload
from .fines import _ledger

router = APIRouter(tags=["approvals"])
APPROVERS = require_roles("circulation", "head_librarian", "admin")
PAY_APPROVERS = require_roles("head_librarian", "admin")


# ------------------------------------------------------------------ schemas
class LoanRequestIn(BaseModel):
    title_id: int
    note: str | None = Field(default=None, max_length=300)


class LoanApproveIn(BaseModel):
    barcode: str | None = None  # pick a specific copy; default is the first free copy
    override_reason: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=300)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ReturnRequestIn(BaseModel):
    loan_id: int
    condition: str = Field(default="ok", pattern="^(ok|damaged)$")
    note: str | None = Field(default=None, max_length=300)


class ReturnApproveIn(BaseModel):
    condition: str | None = Field(default=None, pattern="^(ok|damaged)$")  # staff can correct what the member said
    note: str | None = Field(default=None, max_length=300)


class PaymentRequestIn(BaseModel):
    amount: int = Field(gt=0)
    method: str = Field(pattern="^(cash|bank_transfer|pos)$")
    receipt_no: str = Field(min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=300)


class PaymentApproveIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)


# ------------------------------------------------------------------ helpers
def _brief(m: Member) -> dict:
    return {"id": m.id, "id_number": m.id_number, "name": m.name, "institution_id": m.institution_id}


def _decided(r) -> dict:
    return {"decision_note": r.decision_note, "decided_by_id": r.decided_by_id, "decided_at": iso(r.decided_at)}


def loan_request_out(r: LoanRequest) -> dict:
    return {"id": r.id, "status": r.status, "title_id": r.title_id, "title": r.title.title, "authors": r.title.authors,
            "note": r.note, "loan_id": r.loan_id, "created_at": iso(r.created_at), "member": _brief(r.member), **_decided(r)}


def return_request_out(r: ReturnRequest) -> dict:
    ln = r.loan
    return {"id": r.id, "status": r.status, "loan_id": r.loan_id, "title": ln.copy.title.title, "barcode": ln.copy.barcode,
            "due_at": iso(ln.due_at), "condition": r.condition, "note": r.note, "created_at": iso(r.created_at),
            "member": _brief(r.member), **_decided(r)}


def payment_request_out(r: PaymentRequest) -> dict:
    return {"id": r.id, "status": r.status, "amount": r.amount, "method": r.method, "receipt_no": r.receipt_no,
            "note": r.note, "ledger_id": r.ledger_id, "created_at": iso(r.created_at), "member": _brief(r.member),
            **_decided(r)}


def _fail(e: circ.RuleError):
    raise HTTPException(e.status, {"message": e.message, "code": e.code, "blocks": e.blocks})


def _conflict(message: str, code: str):
    raise HTTPException(409, {"message": message, "code": code, "blocks": []})


def _get_pending(db: Session, model, request_id: int, label: str):
    r = db.get(model, request_id)
    if not r:
        raise HTTPException(404, f"{label} not found.")
    if r.status != "pending":
        _conflict(f"This request was already {r.status}.", "already_decided")
    return r


def _decide(r, actor: Member, status: str, note: str | None) -> None:
    r.status, r.decision_note, r.decided_by_id, r.decided_at = status, note, actor.id, utcnow()


def _listing(db: Session, model, serializer, status: str, page: int, page_size: int, member_id: int | None = None):
    if status != "all" and status not in REQUEST_STATUSES:
        raise HTTPException(422, f"status must be 'all' or one of {', '.join(REQUEST_STATUSES)}")
    stmt = select(model)
    if member_id is not None:
        stmt = stmt.where(model.member_id == member_id)
    if status != "all":
        stmt = stmt.where(model.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(model.id.desc() if status != "pending" else model.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size, "results": [serializer(r) for r in rows]}


# ================================================================== LOAN REQUESTS
@router.post("/loan-requests", status_code=201)
def create_loan_request(body: LoanRequestIn, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    title = db.get(Title, body.title_id)
    if not title or title.retired:
        raise HTTPException(404, "Title not found.")
    cfg, now = settings_store.get_all(db), utcnow()
    blocks = circ.compute_blocks(db, member, cfg, now)
    if blocks:
        raise HTTPException(409, {"message": blocks[0]["message"], "code": "blocked", "blocks": blocks})
    if any(l.copy.title_id == title.id for l in circ.open_loans(db, member.id)):
        _conflict("You already have this title on loan.", "on_loan")
    if db.scalar(select(LoanRequest.id).where(LoanRequest.member_id == member.id, LoanRequest.title_id == title.id,
                                              LoanRequest.status == "pending")):
        _conflict("You already have a pending request for this title.", "duplicate")
    pending = db.scalar(select(func.count()).select_from(LoanRequest)
                        .where(LoanRequest.member_id == member.id, LoanRequest.status == "pending")) or 0
    limit = cfg["max_items"].get(member.member_type, 1)
    if len(circ.open_loans(db, member.id)) + pending >= limit:
        _conflict(f"Item limit reached ({limit}), counting your pending requests.", "limit")
    if not any(c.status == "available" for c in circ.borrowable_copies(db, title.id)):
        _conflict("No copy is on the shelf right now. Place a hold instead.", "unavailable")
    r = LoanRequest(member_id=member.id, title_id=title.id, note=body.note)
    db.add(r)
    db.flush()
    audit(db, member, "loan_request.create", "loan_request", r.id, after={"title_id": title.id})
    db.commit()
    return loan_request_out(r)


@router.get("/loan-requests/mine")
def my_loan_requests(status: str = "all", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     member: Member = Depends(current_member), db: Session = Depends(get_db)):
    return _listing(db, LoanRequest, loan_request_out, status, page, page_size, member.id)


@router.delete("/loan-requests/{request_id}")
def cancel_loan_request(request_id: int, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    r = db.get(LoanRequest, request_id)
    if not r or r.member_id != member.id:
        raise HTTPException(404, "Request not found.")
    if r.status != "pending":
        _conflict(f"This request was already {r.status}.", "already_decided")
    r.status, r.decided_at = "cancelled", utcnow()
    audit(db, member, "loan_request.cancel", "loan_request", r.id)
    db.commit()
    return {"ok": True}


@router.get("/loan-requests")
def list_loan_requests(status: str = "pending", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                       _: Member = Depends(APPROVERS), db: Session = Depends(get_db)):
    return _listing(db, LoanRequest, loan_request_out, status, page, page_size)


@router.post("/loan-requests/{request_id}/approve")
def approve_loan_request(request_id: int, body: LoanApproveIn, actor: Member = Depends(APPROVERS),
                         db: Session = Depends(get_db)):
    """Issues the loan. Rule errors (blocks, copy unavailable...) use the same format as /circulation/issue."""
    r = _get_pending(db, LoanRequest, request_id, "Request")
    if body.barcode:
        copy = db.scalar(select(Copy).where(Copy.barcode == body.barcode.strip().upper()))
        if not copy or copy.title_id != r.title_id:
            raise HTTPException(422, "That barcode is not a copy of the requested title.")
    else:
        copy = db.scalar(select(Copy).where(Copy.title_id == r.title_id, Copy.status == "available",
                                            Copy.collection != "reference_only").order_by(Copy.id))
        if not copy:
            _conflict("No copy is available to issue. Reject the request or wait for a return.", "unavailable")
    now = utcnow()
    try:
        loan = circ.issue(db, r.member, copy, actor, override_reason=body.override_reason, now=now)
    except circ.RuleError as e:
        _fail(e)
    _decide(r, actor, "approved", body.note)
    r.loan_id = loan.id
    notify(db, r.member, "loan_approved", {"title": r.title.title, "due_date": circ.fmt_date(loan.due_at)})
    audit(db, actor, "loan_request.approve", "loan_request", r.id, after={"loan_id": loan.id, "barcode": copy.barcode})
    db.commit()
    return {"request": loan_request_out(r), "loan": loan_out(loan, now)}


@router.post("/loan-requests/{request_id}/reject")
def reject_loan_request(request_id: int, body: ReasonIn, actor: Member = Depends(APPROVERS), db: Session = Depends(get_db)):
    r = _get_pending(db, LoanRequest, request_id, "Request")
    _decide(r, actor, "rejected", body.reason)
    notify(db, r.member, "loan_rejected", {"title": r.title.title, "reason": body.reason})
    audit(db, actor, "loan_request.reject", "loan_request", r.id, after={"reason": body.reason})
    db.commit()
    return loan_request_out(r)


# ================================================================== RETURN REQUESTS
@router.post("/return-requests", status_code=201)
def create_return_request(body: ReturnRequestIn, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    loan = db.get(Loan, body.loan_id)
    if not loan or loan.member_id != member.id:
        raise HTTPException(404, "Loan not found.")
    if loan.returned_at:
        _conflict("This loan is already closed.", "closed")
    if db.scalar(select(ReturnRequest.id).where(ReturnRequest.loan_id == loan.id, ReturnRequest.status == "pending")):
        _conflict("A return is already awaiting confirmation for this loan.", "duplicate")
    r = ReturnRequest(loan_id=loan.id, member_id=member.id, condition=body.condition, note=body.note)
    db.add(r)
    db.flush()
    audit(db, member, "return_request.create", "return_request", r.id, after={"loan_id": loan.id})
    db.commit()
    return return_request_out(r)


@router.get("/return-requests/mine")
def my_return_requests(status: str = "all", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                       member: Member = Depends(current_member), db: Session = Depends(get_db)):
    return _listing(db, ReturnRequest, return_request_out, status, page, page_size, member.id)


@router.get("/return-requests")
def list_return_requests(status: str = "pending", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                         _: Member = Depends(APPROVERS), db: Session = Depends(get_db)):
    return _listing(db, ReturnRequest, return_request_out, status, page, page_size)


@router.post("/return-requests/{request_id}/approve")
def approve_return_request(request_id: int, body: ReturnApproveIn, actor: Member = Depends(APPROVERS),
                           db: Session = Depends(get_db)):
    """Confirms the book is back: closes the loan, calculates the fine and routes the copy (shelf / hold / repair)."""
    r = _get_pending(db, ReturnRequest, request_id, "Request")
    condition, now = body.condition or r.condition, utcnow()
    copy, title = r.loan.copy, r.loan.copy.title.title
    try:
        res = circ.return_copy(db, copy, actor, condition=condition, now=now)
    except circ.RuleError as e:
        _fail(e)
    _decide(r, actor, "approved", body.note)
    r.condition = condition
    notify(db, r.member, "return_confirmed", {"title": title, "amount": circ.fmt_naira(res["fine"])})
    audit(db, actor, "return_request.approve", "return_request", r.id, after={"condition": condition, "fine": res["fine"]})
    db.commit()
    return {"request": return_request_out(r), "return": _return_payload(res, now)}


@router.post("/return-requests/{request_id}/reject")
def reject_return_request(request_id: int, body: ReasonIn, actor: Member = Depends(APPROVERS), db: Session = Depends(get_db)):
    r = _get_pending(db, ReturnRequest, request_id, "Request")
    _decide(r, actor, "rejected", body.reason)
    notify(db, r.member, "return_rejected", {"title": r.loan.copy.title.title, "reason": body.reason})
    audit(db, actor, "return_request.reject", "return_request", r.id, after={"reason": body.reason})
    db.commit()
    return return_request_out(r)


# ================================================================== PAYMENT REQUESTS
def _payment_checks(db: Session, member_id: int, amount: int, receipt_no: str, exclude_id: int | None = None) -> None:
    owed = max(0, circ.member_balance(db, member_id))
    if amount > owed:
        _conflict(f"Payment is more than the amount owed ({circ.fmt_naira(owed)}).", "overpayment")
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.kind == "payment", LedgerEntry.receipt_no == receipt_no)):
        _conflict("This receipt number has already been recorded.", "duplicate_receipt")
    dup = select(PaymentRequest.id).where(PaymentRequest.receipt_no == receipt_no, PaymentRequest.status == "pending")
    if exclude_id:
        dup = dup.where(PaymentRequest.id != exclude_id)
    if db.scalar(dup):
        _conflict("A pending payment with this receipt number already exists.", "duplicate_receipt")


@router.post("/fines/payment-requests", status_code=201)
def create_payment_request(body: PaymentRequestIn, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    receipt = body.receipt_no.strip()
    _payment_checks(db, member.id, body.amount, receipt)
    r = PaymentRequest(member_id=member.id, amount=body.amount, method=body.method, receipt_no=receipt, note=body.note)
    db.add(r)
    db.flush()
    audit(db, member, "payment_request.create", "payment_request", r.id, after={"amount": body.amount, "receipt": receipt})
    db.commit()
    return payment_request_out(r)


@router.get("/fines/payment-requests/mine")
def my_payment_requests(status: str = "all", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                        member: Member = Depends(current_member), db: Session = Depends(get_db)):
    return _listing(db, PaymentRequest, payment_request_out, status, page, page_size, member.id)


@router.get("/fines/payment-requests")
def list_payment_requests(status: str = "pending", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                          _: Member = Depends(PAY_APPROVERS), db: Session = Depends(get_db)):
    return _listing(db, PaymentRequest, payment_request_out, status, page, page_size)


@router.post("/fines/payment-requests/{request_id}/approve")
def approve_payment_request(request_id: int, body: PaymentApproveIn, actor: Member = Depends(PAY_APPROVERS),
                            db: Session = Depends(get_db)):
    """Writes the payment to the member's ledger. Re-checks the amount owed at approval time."""
    r = _get_pending(db, PaymentRequest, request_id, "Request")
    _payment_checks(db, r.member_id, r.amount, r.receipt_no, exclude_id=r.id)
    e = LedgerEntry(member_id=r.member_id, kind="payment", amount=r.amount, method=r.method, receipt_no=r.receipt_no,
                    officer_id=actor.id, note=f"Payment approved (request #{r.id})")
    db.add(e)
    db.flush()
    _decide(r, actor, "approved", body.note)
    r.ledger_id = e.id
    notify(db, r.member, "payment_approved", {"amount": circ.fmt_naira(r.amount), "receipt": r.receipt_no})
    audit(db, actor, "payment_request.approve", "payment_request", r.id,
          after={"member": r.member.id_number, "amount": r.amount, "receipt": r.receipt_no})
    db.commit()
    return {"request": payment_request_out(r), "ledger": _ledger(db, r.member)}


@router.post("/fines/payment-requests/{request_id}/reject")
def reject_payment_request(request_id: int, body: ReasonIn, actor: Member = Depends(PAY_APPROVERS),
                           db: Session = Depends(get_db)):
    r = _get_pending(db, PaymentRequest, request_id, "Request")
    _decide(r, actor, "rejected", body.reason)
    notify(db, r.member, "payment_rejected",
           {"amount": circ.fmt_naira(r.amount), "receipt": r.receipt_no, "reason": body.reason})
    audit(db, actor, "payment_request.reject", "payment_request", r.id, after={"reason": body.reason})
    db.commit()
    return payment_request_out(r)


# ================================================================== SUMMARY
@router.get("/approvals/summary")
def approvals_summary(_: Member = Depends(any_staff), db: Session = Depends(get_db)):
    """Counts for an admin 'needs attention' badge."""
    from ..models import Institution
    n = lambda model: db.scalar(select(func.count()).select_from(model).where(model.status == "pending")) or 0  # noqa: E731
    out = {"loan_requests": n(LoanRequest), "return_requests": n(ReturnRequest), "payment_requests": n(PaymentRequest),
           "institutions": n(Institution)}
    out["total"] = sum(out.values())
    return out
