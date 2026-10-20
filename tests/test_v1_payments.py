import json
import uuid

from sqlalchemy import func, select

from app.config import get_config
from app.database import SessionLocal
from app.models import AuditLog, Checkout, LedgerEntry, Loan, Member, Notification, Payment, WebhookEvent
from app.services import checkout as checkout_svc
from app.services import payment_flow
from app.services.providers import sign
from app.services.providers.sandbox import SandboxProvider
from app.timeutil import utcnow

from .conftest import login
from .test_v1_checkout import add_charge, backdate_loan, copy_status, fresh_member, slug_of
from .test_v1_library import V1, issue, make_title

HOOK = f"{V1}/webhooks/payments/sandbox"


def settle(ref, status, *, emit=True, message=None):
    """Plays the part of the hosted pay page: records the outcome on the sandbox (provider) side only."""
    with SessionLocal() as db:
        p = db.scalar(select(Payment).where(Payment.reference == ref))
        SandboxProvider(db).settle(p, status, channel="card", message=message, emit=emit)
        db.commit()


def reopen(ref):
    with SessionLocal() as db:
        payment_flow.reopen(db, db.scalar(select(Payment).where(Payment.reference == ref)))
        db.commit()


def post_hook(client, body: dict, key=None, sig=None):
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = sig if sig is not None else sign(raw, key or get_config().webhook_signing_key)
    return client.post(HOOK, content=raw, headers={"x-nitt-signature": sig, "content-type": "application/json"})


def pending_fine_checkout(client, desk, head, cataloguer):
    """A member with N200 of overdue fines and a basket waiting on payment. Returns everything a test needs."""
    idn, h = fresh_member(client, head)
    loan = issue(client, desk, idn, make_title(client, cataloguer)["id"])
    backdate_loan(loan["id"], 4)
    want = make_title(client, cataloguer)
    out = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug_of(client, want)], "payFines": True}).json()
    assert out["status"] == "awaiting_payment", out
    return {"idn": idn, "h": h, "want": want, "checkout": out["id"], "ref": out["payment"]["reference"]}


def ledger_payments(ref):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "payment",
                                                                             LedgerEntry.receipt_no == ref))


def loans_of(checkout_id):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Loan).where(Loan.checkout_id == checkout_id))


def test_verify_settles_payment_checkout_and_fines_once(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    settle(s["ref"], "success", emit=False)  # the provider took the money; nothing has told the library yet

    still = client.get(f"{V1}/checkouts/{s['checkout']}", headers=s["h"]).json()
    assert still["status"] == "awaiting_payment" and loans_of(s["checkout"]) == 0  # the browser alone confirms nothing
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 20000

    r = client.post(f"{V1}/payments/{s['ref']}/verify", headers=s["h"])
    assert r.status_code == 200, r.text
    pay = r.json()
    assert pay["status"] == "success" and pay["feeKobo"] == 300 and pay["netKobo"] == 19700
    assert pay["checkoutStatus"] == "confirmed" and pay["channel"] == "card"
    fines = client.get(f"{V1}/me/fines", headers=s["h"]).json()
    assert fines["outstandingKobo"] == 0 and fines["paidKobo"] == 20000 and fines["items"][0]["status"] == "paid"
    assert fines["payments"][0]["reference"] == s["ref"] and fines["payments"][0]["status"] == "success"
    assert copy_status(client, cataloguer, s["want"]) == ["on_loan"]
    assert loans_of(s["checkout"]) == 1

    again = client.post(f"{V1}/payments/{s['ref']}/verify", headers=s["h"])  # nothing changes the second time
    assert again.status_code == 200 and again.json()["status"] == "success"
    assert ledger_payments(s["ref"]) == 1 and loans_of(s["checkout"]) == 1
    assert [p["reference"] for p in client.get(f"{V1}/me/payments", headers=s["h"]).json()["data"]] == [s["ref"]]


def test_webhook_settles_ignores_duplicates_and_bad_signatures(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    settle(s["ref"], "success", emit=False)
    body = {"id": f"evt_{uuid.uuid4().hex[:10]}", "event": "charge.success", "data": {"reference": s["ref"]}}

    assert post_hook(client, body, sig="0" * 128).status_code == 401  # wrong signature: nothing happens
    assert client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()["status"] == "pending"
    with SessionLocal() as db:
        assert not db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == body["id"]))
    assert post_hook(client, body, key="not-the-secret").status_code == 401

    first = post_hook(client, body)
    assert first.status_code == 200 and first.json()["status"] == "received"
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "success" and pay["checkoutStatus"] == "confirmed"

    dup = post_hook(client, body)  # delivering the same event again changes nothing
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    assert ledger_payments(s["ref"]) == 1 and loans_of(s["checkout"]) == 1
    with SessionLocal() as db:
        ev = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == body["id"]))
        assert ev.attempts == 2 and ev.processed_at is not None and len(ev.signature) <= 17

    assert client.post(f"{V1}/webhooks/payments/paystack", content=b"{}").status_code == 404


def test_signed_webhook_cannot_force_a_success_the_provider_denies(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)  # the sandbox never took any money
    body = {"id": f"evt_{uuid.uuid4().hex[:10]}", "event": "charge.success", "data": {"reference": s["ref"]}}
    assert post_hook(client, body).status_code == 200
    assert client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()["status"] == "pending"
    assert loans_of(s["checkout"]) == 0 and ledger_payments(s["ref"]) == 0


def test_failed_payment_keeps_fines_open_and_can_be_retried(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    settle(s["ref"], "failed", message="Insufficient funds")
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "failed" and pay["gatewayResponse"] == "Insufficient funds"
    assert pay["checkoutStatus"] == "payment_failed" and loans_of(s["checkout"]) == 0
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 20000
    assert copy_status(client, cataloguer, s["want"]) == ["reserved"]  # still held until the checkout expires

    reopen(s["ref"])
    assert client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()["status"] == "pending"
    settle(s["ref"], "success")
    done = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert done["status"] == "success" and done["checkoutStatus"] == "confirmed" and loans_of(s["checkout"]) == 1
    labels = [e["label"] for e in done["events"]]
    assert "Retry started" in labels and any(l.startswith("Payment failed") for l in labels)


def test_refund_reopens_fines_and_is_audited(client, desk, head, cataloguer, student):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    settle(s["ref"], "success")
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 0
    finance = login(client, "STF005")

    assert client.post(f"{V1}/payments/{s['ref']}/refund", headers=s["h"]).status_code == 403
    r = client.post(f"{V1}/payments/{s['ref']}/refund", headers=finance, json={"reason": "Paid twice"})
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 20000
    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditLog.action).where(AuditLog.entity == "payment",
                                                               AuditLog.entity_id == s["ref"])))
        assert {"payment.success", "payment.refund", "payment.refund.request"} <= actions
        assert db.scalar(select(Notification.id).where(Notification.template == "payment_refunded",
                                                       Notification.dedupe_key == f"refund:{s['ref']}"))

    again = client.post(f"{V1}/payments/{s['ref']}/refund", headers=finance)
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 20000  # not reopened twice

    t = pending_fine_checkout(client, desk, head, cataloguer)
    settle(t["ref"], "failed")
    assert client.post(f"{V1}/payments/{t['ref']}/refund", headers=finance).status_code == 409


def test_payment_after_checkout_expiry_is_credited_and_flagged(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    with SessionLocal() as db:
        db.get(Checkout, s["checkout"]).expires_at = utcnow().replace(year=2000)
        db.commit()
    checkout_svc.expire_job()
    assert copy_status(client, cataloguer, s["want"]) == ["available"]

    settle(s["ref"], "success")
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "success" and pay["checkoutStatus"] == "expired"
    assert loans_of(s["checkout"]) == 0 and ledger_payments(s["ref"]) == 1
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 0
    with SessionLocal() as db:
        head_id = db.scalar(select(Member.id).where(Member.id_number == "STF002"))
        assert db.scalar(select(Notification.id).where(Notification.member_id == head_id,
                                                       Notification.template == "payment_late",
                                                       Notification.dedupe_key.like(f"%{s['ref']}%")))


def test_manual_payment_links_are_finance_only_and_clear_fines(client, head, student):
    finance = login(client, "STF005")
    idn, h = fresh_member(client, head)
    add_charge(idn, 300)
    link = {"amountKobo": 30000, "email": "a@b.co", "purpose": "Desk fine payment",
            "metadata": {"kind": "fine_payment", "member": idn}}
    assert client.post(f"{V1}/payments/initialize", headers=student, json=link).status_code == 403
    assert client.post(f"{V1}/payments/initialize", json=link).status_code == 401
    low = client.post(f"{V1}/payments/initialize", headers=finance, json={**link, "amountKobo": 4999})
    assert low.status_code == 422 and "amountKobo" in low.json()["error"]["fields"]
    ghost = {**link, "metadata": {"kind": "fine_payment", "member": "NOBODY/1"}}
    assert client.post(f"{V1}/payments/initialize", headers=finance, json=ghost).status_code == 422

    r = client.post(f"{V1}/payments/initialize", headers={**finance, "Idempotency-Key": "pl-1"}, json=link)
    assert r.status_code == 201
    ref = r.json()["reference"]
    assert r.json()["authorizationUrl"] == f"/pay/{ref}"
    assert client.post(f"{V1}/payments/initialize", headers={**finance, "Idempotency-Key": "pl-1"},
                       json=link).json()["reference"] == ref

    settle(ref, "success")
    assert client.get(f"{V1}/me/fines", headers=h).json()["outstandingKobo"] == 0


def test_payments_are_private_to_their_owner(client, desk, head, cataloguer, student):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    assert client.get(f"{V1}/payments/{s['ref']}", headers=student).status_code == 404
    assert client.post(f"{V1}/payments/{s['ref']}/verify", headers=student).status_code == 404
    assert all(p["reference"] != s["ref"] for p in client.get(f"{V1}/me/payments", headers=student).json()["data"])
    assert client.get(f"{V1}/payments/{s['ref']}", headers=desk).status_code == 200  # staff can look
