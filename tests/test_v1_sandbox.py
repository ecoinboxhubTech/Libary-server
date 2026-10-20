import logging
import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_config
from app.database import SessionLocal, engine
from app.models import Payment, PaymentEvent, SandboxTransfer, WebhookEvent
from app.services.sandbox_cards import luhn_ok
from app.timeutil import utcnow

from .conftest import login
from .test_v1_checkout import copy_status, fresh_member, slug_of
from .test_v1_library import V1, make_title
from .test_v1_payments import loans_of, pending_fine_checkout

SB = f"{V1}/sandbox/transactions"
GOOD = {"number": "4242 4242 4242 4242", "exp": "12/99", "cvv": "123"}


def charge(client, finance, amount=30000):
    """A plain test charge (no member, no fines) made the way the dashboard's 'Create a test charge' does."""
    r = client.post(f"{V1}/payments/initialize", headers=finance,
                    json={"amountKobo": amount, "email": "t@student.nitt.edu.ng", "purpose": "Test charge"})
    assert r.status_code == 201, r.text
    return r.json()["reference"]


def pay_card(client, ref, **card):
    return client.post(f"{SB}/{ref}/card", json={**GOOD, **card})


def status_of(client, ref):
    return client.get(f"{SB}/{ref}").json()["status"]


@pytest.fixture(scope="module")
def finance(client):
    return login(client, "STF005")


def test_test_cards_are_luhn_valid():
    for n in ("4242424242424242", "4000000000003220", "4000000000009995", "4000000000000002",
              "4000000000000069", "4000000000000119"):
        assert luhn_ok(n), n


def test_fetch_transaction(client, finance):
    ref = charge(client, finance, 12500)
    t = client.get(f"{SB}/{ref}").json()
    assert t["amountKobo"] == 12500 and t["status"] == "pending" and t["purpose"] == "Test charge"
    assert t["transfer"] is None and t["awaitingOtp"] is False and t["attemptsLeft"] == 5
    assert client.get(f"{SB}/NITT_NOPE0000").status_code == 404


@pytest.mark.parametrize("number,outcome,message", [
    ("4242 4242 4242 4242", "success", None),
    ("4000 0000 0000 9995", "fail", "Insufficient funds"),
    ("4000 0000 0000 0002", "fail", "Declined by bank"),
    ("4000 0000 0000 0069", "fail", "Expired card"),
    ("4000 0000 0000 0119", "fail", "Processing error"),
    ("5555 5555 5555 4444", "success", None),  # any other valid card succeeds
])
def test_card_outcome_table(client, finance, number, outcome, message):
    ref = charge(client, finance)
    r = pay_card(client, ref, number=number)
    assert r.status_code == 200 and r.json()["status"] == outcome
    if message:
        assert r.json()["message"] == message
    assert status_of(client, ref) == ("success" if outcome == "success" else "failed")


def test_card_validation_order_and_attempt_limit(client, finance):
    ref = charge(client, finance)
    assert pay_card(client, ref, number="4242 4242 4242 4241").json()["message"] == "Invalid card number"  # Luhn first
    assert pay_card(client, ref, exp="01/20").json()["message"].startswith("Card has expired")
    assert pay_card(client, ref, exp="13/30").json()["message"].startswith("Card has expired")
    assert pay_card(client, ref, cvv="12").json()["message"] == "Invalid security code"
    r = pay_card(client, ref, number="1111", exp="01/20", cvv="1")  # several problems: the number is reported first
    assert r.json()["message"] == "Invalid card number" and r.json()["attemptsLeft"] == 0
    assert status_of(client, ref) == "pending"  # a typo is not a declined payment
    blocked = pay_card(client, ref)  # the sixth attempt, even with a good card
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "RATE_LIMITED"


def test_good_card_pays_fines_and_confirms_checkout(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    r = pay_card(client, s["ref"])
    assert r.json()["status"] == "success"
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "success" and pay["checkoutStatus"] == "confirmed" and pay["channel"] == "card"
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 0
    assert copy_status(client, cataloguer, s["want"]) == ["on_loan"]
    assert pay_card(client, s["ref"]).status_code == 409  # already paid


def test_declined_card_then_retry_with_a_good_card(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    assert pay_card(client, s["ref"], number="4000 0000 0000 9995").json()["message"] == "Insufficient funds"
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "failed" and pay["checkoutStatus"] == "payment_failed"
    assert loans_of(s["checkout"]) == 0
    assert client.get(f"{V1}/me/fines", headers=s["h"]).json()["outstandingKobo"] == 20000

    assert pay_card(client, s["ref"]).json()["status"] == "success"  # same reference, good card
    done = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert done["status"] == "success" and done["checkoutStatus"] == "confirmed" and loans_of(s["checkout"]) == 1


def test_otp_card_succeeds_with_the_right_code(client, finance):
    ref = charge(client, finance)
    r = pay_card(client, ref, number="4000 0000 0000 3220")
    assert r.json()["status"] == "otp" and status_of(client, ref) == "pending"
    assert client.get(f"{SB}/{ref}").json()["awaitingOtp"] is True
    wrong = client.post(f"{SB}/{ref}/otp", json={"otp": "000000"})
    assert wrong.json()["status"] == "otp" and "2 attempt" in wrong.json()["message"]
    assert client.post(f"{SB}/{ref}/otp", json={"otp": "123456"}).json()["status"] == "success"
    assert status_of(client, ref) == "success"


def test_otp_fails_after_three_wrong_codes(client, finance):
    ref = charge(client, finance)
    pay_card(client, ref, number="4000 0000 0000 3220")
    assert client.post(f"{SB}/{ref}/otp", json={"otp": "1"}).json()["status"] == "otp"
    assert client.post(f"{SB}/{ref}/otp", json={"otp": "2"}).json()["status"] == "otp"
    last = client.post(f"{SB}/{ref}/otp", json={"otp": "3"})
    assert last.json()["status"] == "fail" and status_of(client, ref) == "failed"
    assert client.post(f"{SB}/{ref}/otp", json={"otp": "123456"}).status_code == 409  # nothing pending any more
    assert client.post(f"{SB}/{charge(client, finance)}/otp", json={"otp": "123456"}).status_code == 409


def test_bank_transfer_succeeds_inside_the_window(client, desk, head, cataloguer):
    s = pending_fine_checkout(client, desk, head, cataloguer)
    assert client.post(f"{SB}/{s['ref']}/transfer/confirm").status_code == 409  # not started
    t = client.post(f"{SB}/{s['ref']}/transfer").json()
    assert len(t["rrr"]) == 12 and t["bank"] and len(t["account"]) == 10 and s["ref"] in t["accountName"]
    assert client.post(f"{SB}/{s['ref']}/transfer").json() == t  # still valid: same details
    assert client.get(f"{SB}/{s['ref']}").json()["transfer"] == t
    assert client.post(f"{SB}/{s['ref']}/transfer/confirm").json()["status"] == "success"
    pay = client.get(f"{V1}/payments/{s['ref']}", headers=s["h"]).json()
    assert pay["status"] == "success" and pay["channel"] == "bank_transfer" and pay["checkoutStatus"] == "confirmed"
    assert client.post(f"{SB}/{s['ref']}/transfer/confirm").json()["status"] == "success"  # idempotent


def test_bank_transfer_fails_after_the_window_or_when_expired(client, finance):
    late = charge(client, finance)
    client.post(f"{SB}/{late}/transfer")
    with SessionLocal() as db:
        pid = db.scalar(select(Payment.id).where(Payment.reference == late))
        db.get(SandboxTransfer, pid).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    r = client.post(f"{SB}/{late}/transfer/confirm")
    assert r.json() == {"status": "fail", "message": "Transfer window expired"} and status_of(client, late) == "failed"

    # the customer can start again on the same reference and pay
    fresh = client.post(f"{SB}/{late}/transfer").json()
    assert fresh["rrr"] and status_of(client, late) == "pending"
    assert client.post(f"{SB}/{late}/transfer/confirm").json()["status"] == "success"

    forced = charge(client, finance)
    client.post(f"{SB}/{forced}/transfer")
    assert client.post(f"{SB}/{forced}/transfer/expire").json()["status"] == "fail"
    assert status_of(client, forced) == "failed"
    assert client.post(f"{SB}/{forced}/transfer/expire").status_code == 409


def test_card_details_never_reach_the_database_or_logs(client, finance, caplog):
    numbers = ["4242424242424242", "4000000000003220", "4000000000009995", "1234567812345678", "5555555555554444"]
    with caplog.at_level(logging.DEBUG):
        for n in numbers:
            ref = charge(client, finance)
            r = pay_card(client, ref, number=" ".join(n[i:i + 4] for i in range(0, 16, 4)), cvv="987")
            assert r.status_code == 200
        bad = client.post(f"{SB}/{ref}/card", json={"number": "4242424242424242"})  # 422: must not echo the number
        assert bad.status_code == 422 and "4242" not in bad.text
    engine.dispose()  # make sure everything written has reached the files scanned below
    path = engine.url.database
    blobs = b"".join(open(f, "rb").read() for f in (path, path + "-wal") if os.path.exists(f))
    for n in numbers:
        for form in (n, " ".join(n[i:i + 4] for i in range(0, 16, 4))):
            assert form not in caplog.text, f"{form} appeared in a log line"
            assert form.encode() not in blobs, f"{form} appeared in the database"
    with SessionLocal() as db:
        labels = " ".join(db.scalars(select(PaymentEvent.label)))
    assert "4242" not in labels and "987" not in labels


def test_sandbox_admin_requires_finance_or_admin(client, student, desk):
    for path in ("stats", "transactions", "webhooks", "keys"):
        assert client.get(f"{V1}/sandbox/admin/{path}").status_code == 401
        assert client.get(f"{V1}/sandbox/admin/{path}", headers=student).status_code == 403
        assert client.get(f"{V1}/sandbox/admin/{path}", headers=desk).status_code == 403
    assert client.post(f"{V1}/sandbox/admin/reset", headers=student, json={"confirm": True}).status_code == 403


def test_sandbox_admin_dashboard_data(client, finance):
    ok, bad = charge(client, finance, 100000), charge(client, finance, 100000)
    pay_card(client, ok)
    pay_card(client, bad, number="4000 0000 0000 0002")
    s = client.get(f"{V1}/sandbox/admin/stats", headers=finance).json()
    assert s["netKobo"] == s["grossKobo"] - s["feesKobo"] and s["success"] >= 1 and s["failed"] >= 1
    assert 0 < s["ratePercent"] < 100 and {"refundedKobo"} <= set(s)

    rows = client.get(f"{V1}/sandbox/admin/transactions", headers=finance, params={"status": "failed"}).json()
    assert rows["data"] and all(r["status"] == "failed" for r in rows["data"])
    mine = next(r for r in rows["data"] if r["reference"] == bad)
    assert [e["label"] for e in mine["events"]][0] == "Payment created" and mine["gatewayResponse"] == "Declined by bank"
    assert client.get(f"{V1}/sandbox/admin/transactions", headers=finance, params={"status": "nope"}).status_code == 422

    log = client.get(f"{V1}/sandbox/admin/webhooks", headers=finance).json()
    ev = next(e for e in log["data"] if e["reference"] == ok and e["event"] == "charge.success")
    assert ev["payload"]["data"]["amount"] == 100000 and ev["payload"]["data"]["fees"] == 2000 - 500 and ev["httpStatus"] == 200
    assert ev["attempts"] == 1 and ev["processedAt"]
    again = client.post(f"{V1}/sandbox/admin/webhooks/{ev['id']}/resend", headers=finance).json()
    assert again["attempts"] == 2
    assert client.post(f"{V1}/sandbox/admin/webhooks/999999/resend", headers=finance).status_code == 404

    keys = client.get(f"{V1}/sandbox/admin/keys", headers=finance).json()
    assert keys["publicKey"] == get_config().payment_public_key and keys["secretKey"].startswith("sk_test_")
    assert get_config().webhook_signing_key not in str(keys)


def test_sandbox_is_off_for_other_providers(client, finance, monkeypatch):
    ref = charge(client, finance)
    monkeypatch.setattr(get_config(), "payment_provider", "paystack")
    assert client.get(f"{SB}/{ref}").status_code == 404
    assert client.get(f"{V1}/sandbox/admin/stats", headers=finance).status_code == 404


def test_reset_needs_confirmation_is_blocked_in_production_and_reseeds(client, finance, monkeypatch):
    ref = charge(client, finance)
    assert client.post(f"{V1}/sandbox/admin/reset", headers=finance, json={}).status_code == 422
    monkeypatch.setattr(get_config(), "app_env", "production")
    assert client.post(f"{V1}/sandbox/admin/reset", headers=finance, json={"confirm": True}).status_code == 403
    monkeypatch.undo()

    r = client.post(f"{V1}/sandbox/admin/reset", headers=finance, json={"confirm": True})
    assert r.status_code == 200 and r.json()["seeded"] == 4 and r.json()["deletedPayments"] >= 1
    assert client.get(f"{SB}/{ref}").status_code == 404  # wiped
    rows = client.get(f"{V1}/sandbox/admin/transactions", headers=finance, params={"limit": 50}).json()
    assert rows["total"] == 4
    assert sorted(r["status"] for r in rows["data"]) == ["failed", "refunded", "success", "success"]
    assert client.get(f"{V1}/sandbox/admin/stats", headers=finance).json()["refundedKobo"] == 50000
    with SessionLocal() as db:
        assert db.scalar(select(WebhookEvent.id).where(WebhookEvent.reference == ref)) is None
