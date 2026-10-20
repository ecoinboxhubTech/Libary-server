import uuid
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditLog, Checkout, LedgerEntry, Loan, Member
from app.services import checkout as checkout_svc
from app.timeutil import utcnow

from .test_v1_library import V1, issue, make_title

PW = "Passw0rd!x"


def fresh_member(client, head, **kw):
    """A brand-new student with a usable session (the temporary password is changed straight away)."""
    idn = f"CO/{uuid.uuid4().hex[:6].upper()}"
    r = client.post("/api/members", headers=head, json={
        "id_number": idn, "name": "Checkout Tester", "member_type": "student", "temporary_password": PW,
        "email": f"{idn.lower().replace('/', '.')}@example.com", **kw})
    assert r.status_code == 200, r.text
    tok = client.post("/api/auth/login", json={"id_number": idn, "password": PW}).json()["access_token"]
    r = client.post("/api/auth/change-password", headers={"Authorization": f"Bearer {tok}"},
                    json={"current_password": PW, "new_password": "Another#Pass9"})
    assert r.status_code == 200, r.text
    return idn, {"Authorization": f"Bearer {r.json()['access_token']}"}


def slug_of(client, title):
    return client.get(f"{V1}/titles/{title['id']}").json()["id"]


def copy_status(client, cataloguer, title):
    return [c["status"] for c in client.get(f"/api/catalogue/titles/{title['id']}", headers=cataloguer).json()["copies"]]


def add_charge(idn, naira, kind="adjustment"):
    with SessionLocal() as db:
        m = db.scalar(select(Member).where(Member.id_number == idn))
        e = LedgerEntry(member_id=m.id, kind=kind, amount=naira, note="Test charge")
        db.add(e)
        db.commit()
        return e.id


def backdate_loan(loan_id, days):
    with SessionLocal() as db:
        db.get(Loan, loan_id).due_at = utcnow() - timedelta(days=days)
        db.commit()


def test_checkout_without_fines_confirms_loans(client, head, cataloguer):
    idn, h = fresh_member(client, head)
    a, b = make_title(client, cataloguer), make_title(client, cataloguer)
    body = {"items": [slug_of(client, a), slug_of(client, b)], "collectionPoint": "desk", "periodDays": 7,
            "payFines": True, "paymentMethod": "online"}
    r = client.post(f"{V1}/checkouts", headers={**h, "Idempotency-Key": "k-1"}, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "confirmed" and out["finesToPayKobo"] == 0 and "payment" not in out
    assert len(out["loans"]) == 2 and {"id", "title", "titleId", "dueAt"} == set(out["loans"][0])  # no barcodes for members
    assert copy_status(client, cataloguer, a) == ["on_loan"] and copy_status(client, cataloguer, b) == ["on_loan"]
    with SessionLocal() as db:
        due = db.scalar(select(Loan.due_at).where(Loan.checkout_id == out["id"]))
        assert timedelta(days=6) < due - utcnow() < timedelta(days=10)  # 7 days, rolled past closure days

    again = client.post(f"{V1}/checkouts", headers={**h, "Idempotency-Key": "k-1"}, json=body)
    assert again.status_code == 201 and again.json()["id"] == out["id"]
    assert again.headers["Idempotent-Replay"] == "true"
    assert len(client.get(f"{V1}/me/loans", headers=h).json()["data"]) == 2


def test_checkout_with_fines_creates_exact_payment(client, desk, head, cataloguer):
    idn, h = fresh_member(client, head)
    old = make_title(client, cataloguer)
    loan = issue(client, desk, idn, old["id"])
    backdate_loan(loan["id"], 4)  # 4 days x N50 = N200

    fines = client.get(f"{V1}/me/fines", headers=h).json()
    assert fines["perDayKobo"] == 5000 and fines["outstandingKobo"] == 20000 and fines["paidKobo"] == 0
    assert fines["items"][0]["daysOverdue"] == 4 and fines["items"][0]["loanId"] == f"ln_{loan['id']}"

    want = make_title(client, cataloguer)
    body = {"items": [slug_of(client, want)], "payFines": True, "paymentMethod": "online", "amountKobo": 1}
    r = client.post(f"{V1}/checkouts", headers=h, json=body)  # the amount in the body is ignored
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "awaiting_payment" and out["finesToPayKobo"] == 20000
    ref = out["payment"]["reference"]
    assert ref.startswith("NITT_") and out["payment"]["authorizationUrl"] == f"/pay/{ref}"
    assert copy_status(client, cataloguer, want) == ["reserved"]
    assert client.get(f"{V1}/me/fines", headers=h).json()["outstandingKobo"] == 20000  # nothing paid yet

    second = client.post(f"{V1}/checkouts", headers=h, json=body)
    assert second.status_code == 409 and second.json()["error"]["fields"]["checkoutId"] == out["id"]

    gone = client.delete(f"{V1}/checkouts/{out['id']}", headers=h)
    assert gone.status_code == 200 and gone.json()["status"] == "cancelled"
    assert copy_status(client, cataloguer, want) == ["available"]
    assert client.delete(f"{V1}/checkouts/{out['id']}", headers=h).status_code == 409

    # fines under the block threshold do not stop a checkout when the member is not paying them now
    ok = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug_of(client, want)]})
    assert ok.status_code == 201 and ok.json()["status"] == "confirmed"


def test_checkout_blocked_by_large_fines_unless_paid(client, head, cataloguer):
    idn, h = fresh_member(client, head)
    add_charge(idn, 1500)
    t = make_title(client, cataloguer)
    body = {"items": [slug_of(client, t)]}
    r = client.post(f"{V1}/checkouts", headers=h, json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FINES_BLOCK"
    assert "₦1,000" in r.json()["error"]["message"]
    assert copy_status(client, cataloguer, t) == ["available"]

    paying = client.post(f"{V1}/checkouts", headers=h, json={**body, "payFines": True})
    assert paying.status_code == 201 and paying.json()["finesToPayKobo"] == 150000
    # a desk-payment choice does not lift the block
    client.delete(f"{V1}/checkouts/{paying.json()['id']}", headers=h)
    desk_choice = client.post(f"{V1}/checkouts", headers=h, json={**body, "payFines": True, "paymentMethod": "desk"})
    assert desk_choice.status_code == 409 and desk_choice.json()["error"]["code"] == "FINES_BLOCK"


def test_checkout_limits_and_availability(client, head, cataloguer):
    idn, h = fresh_member(client, head)
    many = [slug_of(client, make_title(client, cataloguer)) for _ in range(4)]
    r = client.post(f"{V1}/checkouts", headers=h, json={"items": many})
    assert r.status_code == 409 and r.json()["error"]["code"] == "LOAN_LIMIT"

    t = make_title(client, cataloguer)
    copy = client.get(f"/api/catalogue/titles/{t['id']}", headers=cataloguer).json()["copies"][0]
    client.patch(f"/api/catalogue/copies/{copy['id']}", headers=cataloguer, json={"status": "damaged"})
    r = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug_of(client, t)]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_COPY_AVAILABLE"

    assert client.post(f"{V1}/checkouts", headers=h, json={"items": many[:1], "periodDays": 3}).status_code == 422
    assert client.post(f"{V1}/checkouts", headers=h, json={"items": []}).status_code == 422
    assert client.post(f"{V1}/checkouts", headers=h, json={"items": ["no-such-title"]}).status_code == 404


def test_unpaid_checkout_expires_and_frees_copies(client, head, cataloguer):
    idn, h = fresh_member(client, head)
    add_charge(idn, 300)
    t = make_title(client, cataloguer)
    out = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug_of(client, t)], "payFines": True}).json()
    assert out["status"] == "awaiting_payment" and copy_status(client, cataloguer, t) == ["reserved"]

    with SessionLocal() as db:
        db.get(Checkout, out["id"]).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert checkout_svc.expire_job() >= 1
    assert client.get(f"{V1}/checkouts/{out['id']}", headers=h).json()["status"] == "expired"
    assert copy_status(client, cataloguer, t) == ["available"]


def test_checkout_is_private_to_its_owner(client, head, cataloguer, student):
    idn, h = fresh_member(client, head)
    t = make_title(client, cataloguer)
    out = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug_of(client, t)]}).json()
    assert client.get(f"{V1}/checkouts/{out['id']}", headers=student).status_code == 404
    assert client.delete(f"{V1}/checkouts/{out['id']}", headers=student).status_code == 404


def test_waive_fine_is_head_librarian_only_and_audited(client, desk, head, cataloguer):
    idn, h = fresh_member(client, head)
    add_charge(idn, 400)
    item = client.get(f"{V1}/me/fines", headers=h).json()["items"][0]
    assert item["status"] == "open" and item["amountKobo"] == 40000

    assert client.post(f"{V1}/fines/{item['id']}/waive", headers=desk, json={"reason": "Goodwill gesture"}).status_code == 403
    assert client.post(f"{V1}/fines/{item['id']}/waive", headers=h, json={"reason": "Goodwill gesture"}).status_code == 403
    assert client.post(f"{V1}/fines/{item['id']}/waive", headers=head, json={"reason": "x"}).status_code == 422
    ok = client.post(f"{V1}/fines/{item['id']}/waive", headers=head, json={"reason": "Goodwill gesture"})
    assert ok.status_code == 200 and ok.json()["outstandingKobo"] == 0
    after = client.get(f"{V1}/me/fines", headers=h).json()
    assert after["outstandingKobo"] == 0 and after["items"][0]["status"] == "waived"
    assert client.post(f"{V1}/fines/{item['id']}/waive", headers=head, json={"reason": "Goodwill gesture"}).status_code == 409
    with SessionLocal() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "fine.waiver",
                                               AuditLog.entity_id.isnot(None)).order_by(AuditLog.id.desc()))
        assert row.after["fine"] == item["id"] and row.after["reason"] == "Goodwill gesture"
