import uuid

from .conftest import login

PW = "Passw0rd!x"


def _member(client, head, idn):
    r = client.post("/api/members", headers=head, json={"id_number": idn, "name": "Test " + idn, "member_type": "student",
                                                        "temporary_password": PW})
    assert r.status_code == 200, r.text
    return login(client, idn, PW)


def _title(client, cataloguer, copies=1):
    r = client.post("/api/catalogue/titles", headers=cataloguer, json={"title": "Approval Book " + uuid.uuid4().hex[:6], "copies": copies})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["copies"][0]["barcode"]


def test_member_detail(client, head, desk):
    m = client.get("/api/members/lookup/NITT/HND/24/001", headers=head).json()["member"]
    d = client.get(f"/api/members/{m['id']}", headers=head).json()
    assert d["member"]["id_number"] == "NITT/HND/24/001" and d["stats"]["loans_open"] >= 1
    assert {"loans", "loan_history", "holds", "ledger", "blocks", "pending_requests"} <= set(d)
    assert client.get("/api/members/999999", headers=head).status_code == 404


def test_loan_request_approve_and_return_flow(client, head, cataloguer):
    uid = uuid.uuid4().hex[:6].upper()
    s = _member(client, head, "APR" + uid)
    title_id, barcode = _title(client, cataloguer)

    r = client.post("/api/loan-requests", headers=s, json={"title_id": title_id})
    assert r.status_code == 201, r.text
    req = r.json()
    assert client.post("/api/loan-requests", headers=s, json={"title_id": title_id}).status_code == 409  # duplicate
    assert client.get("/api/loan-requests", headers=s).status_code == 403
    assert any(x["id"] == req["id"] for x in client.get("/api/loan-requests", headers=head).json()["results"])

    ok = client.post(f"/api/loan-requests/{req['id']}/approve", json={}, headers=head)
    assert ok.status_code == 200, ok.text
    loan_id = ok.json()["loan"]["id"]
    assert ok.json()["request"]["status"] == "approved"
    assert client.post(f"/api/loan-requests/{req['id']}/approve", json={}, headers=head).status_code == 409

    rr = client.post("/api/return-requests", headers=s, json={"loan_id": loan_id})
    assert rr.status_code == 201, rr.text
    rid = rr.json()["id"]
    done = client.post(f"/api/return-requests/{rid}/approve", json={}, headers=head)
    assert done.status_code == 200, done.text
    assert done.json()["return"]["copy_status"] == "available"
    assert client.get(f"/api/catalogue/copies/by-barcode/{barcode}", headers=head).json()["status"] == "available"


def test_loan_request_reject_and_cancel(client, head, cataloguer):
    s = _member(client, head, "APR" + uuid.uuid4().hex[:6].upper())
    title_id, _ = _title(client, cataloguer)
    a = client.post("/api/loan-requests", headers=s, json={"title_id": title_id}).json()
    assert client.post(f"/api/loan-requests/{a['id']}/reject", json={"reason": "no"}, headers=head).status_code == 422
    assert client.post(f"/api/loan-requests/{a['id']}/reject", json={"reason": "Not eligible"}, headers=head).json()["status"] == "rejected"
    b = client.post("/api/loan-requests", headers=s, json={"title_id": title_id}).json()
    assert client.delete(f"/api/loan-requests/{b['id']}", headers=s).status_code == 200
    mine = client.get("/api/loan-requests/mine", headers=s).json()
    assert {x["status"] for x in mine["results"]} == {"rejected", "cancelled"}


def test_request_unavailable_title(client, head, cataloguer, desk):
    s1 = _member(client, head, "APR" + uuid.uuid4().hex[:6].upper())
    title_id, barcode = _title(client, cataloguer)
    holder = "APR" + uuid.uuid4().hex[:6].upper()
    _member(client, head, holder)
    assert client.post("/api/circulation/issue", json={"member_id_number": holder, "barcode": barcode}, headers=desk).status_code == 200
    r = client.post("/api/loan-requests", headers=s1, json={"title_id": title_id})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "unavailable"


def test_payment_request_needs_admin_approval(client, head, desk, cataloguer):
    # build a fine: borrow in the past through offline sync, return now
    from datetime import datetime, timedelta, timezone
    idn = "APR" + uuid.uuid4().hex[:6].upper()
    s = _member(client, head, idn)
    _, barcode = _title(client, cataloguer)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    res = client.post("/api/circulation/sync", headers=desk, json={"transactions": [
        {"client_id": uuid.uuid4().hex, "type": "issue", "barcode": barcode, "member_id_number": idn, "occurred_at": long_ago},
        {"client_id": uuid.uuid4().hex, "type": "return", "barcode": barcode, "occurred_at": now}]})
    assert [x["status"] for x in res.json()["results"]] == ["ok", "ok"]
    owed = client.get("/api/fines/me", headers=s).json()["owed"]
    assert owed > 0

    too_much = client.post("/api/fines/payment-requests", headers=s,
                           json={"amount": owed + 1, "method": "cash", "receipt_no": "X-" + uuid.uuid4().hex[:6]})
    assert too_much.status_code == 409
    receipt = "TRF-" + uuid.uuid4().hex[:6]
    pr = client.post("/api/fines/payment-requests", headers=s, json={"amount": 50, "method": "bank_transfer", "receipt_no": receipt})
    assert pr.status_code == 201, pr.text
    assert client.get("/api/fines/me", headers=s).json()["owed"] == owed  # nothing posted yet
    assert client.get("/api/fines/payment-requests", headers=desk).status_code == 403  # desk officers cannot approve
    assert client.post(f"/api/fines/payment-requests/{pr.json()['id']}/approve", json={}, headers=desk).status_code == 403
    ok = client.post(f"/api/fines/payment-requests/{pr.json()['id']}/approve", json={}, headers=head)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ledger"]["owed"] == owed - 50

    summary = client.get("/api/approvals/summary", headers=head).json()
    assert {"loan_requests", "return_requests", "payment_requests", "institutions", "total"} <= set(summary)


def test_admin_can_add_books(client):
    admin = login(client, "STF001")
    r = client.post("/api/catalogue/titles", headers=admin, json={
        "title": "Admin Added " + uuid.uuid4().hex[:6], "authors": "A. Admin", "subject_area": "policy", "copies": 2})
    assert r.status_code == 200, r.text
    assert len(r.json()["copies"]) == 2
    tid = r.json()["id"]
    assert client.post(f"/api/catalogue/titles/{tid}/copies", headers=admin, json={"count": 1}).status_code == 200
    assert client.patch(f"/api/catalogue/titles/{tid}", headers=admin, json={"publisher": "NITT Press"}).status_code == 200
    assert client.get("/api/catalogue/import-template", headers=admin).status_code == 200
