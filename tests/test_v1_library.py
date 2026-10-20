import uuid

from .conftest import login

V1 = "/api/v1"


def make_title(client, h, copies=1, **kw):
    body = {"title": f"V1 Test {uuid.uuid4().hex[:6]}", "authors": "A. Tester", "copies": copies,
            "shelf_location": "V1 / 1", "subject_area": "road", **kw}
    r = client.post("/api/catalogue/titles", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def issue(client, desk, idn, title_id):
    """Issue the first available copy of a title through the desk API; returns the loan JSON."""
    t = client.get(f"/api/catalogue/titles/{title_id}", headers=desk).json()
    copy = next(c for c in t["copies"] if c["status"] == "available")
    r = client.post("/api/circulation/issue", headers=desk, json={"member_id_number": idn, "barcode": copy["barcode"]})
    assert r.status_code == 200, r.text
    return r.json()["loan"]


def test_error_format_and_auth(client):
    r = client.get(f"{V1}/me/loans")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = client.get(f"{V1}/titles/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.get(f"{V1}/titles?limit=0")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "limit" in r.json()["error"]["fields"]
    # the older /api routes keep FastAPI's own error shape
    assert "detail" in client.get("/api/auth/me").json()


def test_search_returns_availability_and_filters(client, cataloguer):
    t = make_title(client, cataloguer, copies=1, title="Port Gantry Cranes Handbook")
    r = client.get(f"{V1}/titles", params={"q": "port"})
    assert r.status_code == 200
    body = r.json()
    assert {"data", "page", "limit", "total"} <= set(body) and body["total"] >= 1
    mine = next(x for x in body["data"] if x["title"] == "Port Gantry Cranes Handbook")
    assert mine["availability"] == {"total": 1, "available": 1, "isDigital": False}
    assert mine["id"] == "port-gantry-cranes-handbook"

    # take the only copy off the shelf: available=1 must now hide it
    copy = client.get(f"/api/catalogue/titles/{t['id']}", headers=cataloguer).json()["copies"][0]
    client.patch(f"/api/catalogue/copies/{copy['id']}", headers=cataloguer, json={"status": "damaged"})
    shown = client.get(f"{V1}/titles", params={"q": "port gantry cranes", "available": 1}).json()["data"]
    assert all(x["id"] != mine["id"] for x in shown)
    assert all(x["availability"]["available"] > 0 or x["availability"]["isDigital"] for x in shown)
    listed = client.get(f"{V1}/titles", params={"q": "port gantry cranes"}).json()["data"]
    assert any(x["id"] == mine["id"] for x in listed)


def test_title_detail_copies_and_related(client, cataloguer):
    a = make_title(client, cataloguer, copies=2, title="Railway Signalling Basics")
    make_title(client, cataloguer, copies=1, title="Railway Track Maintenance")
    slug = "railway-signalling-basics"
    d = client.get(f"{V1}/titles/{slug}").json()
    assert d["id"] == slug and len(d["copies"]) == 2
    assert {"id", "status", "location"} == set(d["copies"][0])  # members never see barcodes
    assert client.get(f"{V1}/titles/{a['id']}").json()["id"] == slug  # numeric id is accepted too
    rel = client.get(f"{V1}/titles/{slug}/related").json()["data"]
    assert rel and all(x["id"] != slug for x in rel)


def test_loans_renew_return_and_permissions(client, desk, cataloguer, student):
    t = make_title(client, cataloguer, copies=1)
    loan = issue(client, desk, "NITT/HND/24/001", t["id"])
    ref = f"ln_{loan['id']}"

    mine = client.get(f"{V1}/me/loans", headers=student).json()
    row = next(x for x in mine["data"] if x["id"] == ref)
    assert row["canRenew"] is True and row["renewals"] == 0 and row["overdueDays"] == 0

    # another member can neither see nor renew it
    other = login(client, "NITT/HND/24/002")
    assert all(x["id"] != ref for x in client.get(f"{V1}/me/loans", headers=other).json()["data"])
    assert client.post(f"{V1}/loans/{ref}/renew", headers=other).status_code == 404

    r = client.post(f"{V1}/loans/{ref}/renew", headers=student)
    assert r.status_code == 200 and r.json()["renewals"] == 1
    again = client.post(f"{V1}/loans/{ref}/renew", headers=student)  # students get one renewal by default
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"

    assert client.post(f"{V1}/loans/{ref}/return", headers=student).status_code == 403
    done = client.post(f"{V1}/loans/{ref}/return", headers=desk)
    assert done.status_code == 200 and done.json()["fineKobo"] == 0
    assert all(x["id"] != ref for x in client.get(f"{V1}/me/loans", headers=student).json()["data"])


def test_holds_queue_ready_and_cancel(client, desk, cataloguer, student):
    t = make_title(client, cataloguer, copies=1)
    loan = issue(client, desk, "NITT/HND/24/001", t["id"])
    other = login(client, "NITT/HND/24/002")
    slug = client.get(f"{V1}/titles/{t['id']}").json()["id"]

    r = client.post(f"{V1}/holds", headers=other, json={"titleId": slug})
    assert r.status_code == 201, r.text
    hold = r.json()
    assert hold["status"] == "waiting" and hold["queuePosition"] == 1 and hold["readyUntil"] is None
    assert [h["id"] for h in client.get(f"{V1}/me/holds", headers=other).json()["data"]] == [hold["id"]]

    # the borrower's copy comes back: the hold becomes ready for 48 hours
    client.post(f"{V1}/loans/ln_{loan['id']}/return", headers=desk)
    ready = client.get(f"{V1}/me/holds", headers=other).json()["data"][0]
    assert ready["status"] == "ready" and ready["readyUntil"]

    assert client.delete(f"{V1}/holds/{hold['id']}", headers=student).status_code == 404  # not theirs
    gone = client.delete(f"{V1}/holds/{hold['id']}", headers=other)
    assert gone.status_code == 200 and gone.json()["status"] == "cancelled"
    assert client.get(f"{V1}/me/holds", headers=other).json()["data"] == []
