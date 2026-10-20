from sqlalchemy import select

from app.database import SessionLocal
from app.models import Institution
from .conftest import login

LIVE = {"X-API-Key": "nitt_live_demo_zcct_0000000000000000000000"}


def _reg(client, email="new@college.example.com"):
    return client.post("/api/institutions/register", json={
        "name": "Test College", "type": "college", "email": email, "contact_name": "Jo Registrar"})


def test_register_review_and_live_api(client, head):
    r = _reg(client)
    assert r.status_code == 201, r.text
    code, sandbox_key = r.json()["code"], r.json()["sandbox_api_key"]
    assert sandbox_key.startswith("nitt_test_")
    assert _reg(client).status_code == 409  # same email

    st = client.get("/api/institutions/register/status", params={"code": code, "email": "new@college.example.com"})
    assert st.json()["status"] == "pending"

    # sandbox works before approval, live does not
    assert client.get("/api/sandbox/v1/me", headers={"X-API-Key": sandbox_key}).status_code == 200
    with SessionLocal() as db:
        inst_id = db.scalar(select(Institution.id).where(Institution.code == code))
    assert client.get("/api/partner/v1/me", headers={"X-API-Key": sandbox_key}).status_code == 401

    appr = client.post(f"/api/institutions/{inst_id}/approve", json={}, headers=head)
    assert appr.status_code == 200, appr.text
    live = {"X-API-Key": appr.json()["live_api_key"]}
    assert client.get("/api/partner/v1/me", headers=live).json()["mode"] == "live"

    en = client.post("/api/partner/v1/students", headers=live, json={"students": [
        {"id_number": "tc/1", "name": "Test Student", "temporary_password": "Passw0rd!x"},
        {"id_number": "tc/1", "name": "Dup"}]})
    body = en.json()
    assert body["created"] == 1 and len(body["errors"]) == 1
    # the enrolled student can sign in (after the forced password change) and is a normal member
    tok = client.post("/api/auth/login", json={"id_number": "TC/1", "password": "Passw0rd!x"}).json()
    assert tok["member"]["institution_id"] == inst_id

    # suspension cuts off the live API and student borrowing
    assert client.post(f"/api/institutions/{inst_id}/suspend", json={"reason": "testing"}, headers=head).status_code == 200
    assert client.get("/api/partner/v1/me", headers=live).status_code == 403
    assert client.post(f"/api/institutions/{inst_id}/reinstate", headers=head).status_code == 200
    assert client.get("/api/partner/v1/me", headers=live).status_code == 200


def test_staff_endpoints_need_staff(client, student, desk):
    assert client.get("/api/institutions", headers=student).status_code == 403
    assert client.get("/api/institutions", headers=desk).json()["total"] >= 5
    assert client.post("/api/institutions/1/approve", json={}, headers=student).status_code == 403


def test_demo_data_and_blocks(client, desk):
    r = client.get("/api/partner/v1/students", headers=LIVE)
    assert r.status_code == 200 and r.json()["total"] == 3
    loans = client.get("/api/partner/v1/students/ZCCT/24/002/loans", headers=LIVE)
    assert loans.status_code == 200 and len(loans.json()["loans"]) == 1
    # suspended institution's student is blocked at the desk
    s = client.get("/api/members/lookup/SLC/24/001", headers=desk).json()
    assert any(b["code"] == "institution_inactive" for b in s["blocks"])


def test_sandbox_never_persists(client):
    key = {"X-API-Key": "nitt_test_demo_inst0001"}
    r = client.post("/api/sandbox/v1/students", headers=key, json={"students": [
        {"id_number": "A1", "name": "Someone"}, {"id_number": "EXISTS1", "name": "Old"}, {"id_number": "ERR1", "name": "Bad"}]})
    b = r.json()
    assert (b["created"], b["updated"], len(b["errors"])) == (1, 1, 1)
    assert client.get("/api/sandbox/v1/catalogue/search", headers=key, params={"q": "maritime"}).json()["total"] == 1
    assert client.get("/api/sandbox/v1/me").status_code == 401
