import uuid

from .conftest import login

PW = "Passw0rd!x"


def _member(client, head):
    idn = "EBK" + uuid.uuid4().hex[:6].upper()
    r = client.post("/api/members", headers=head, json={"id_number": idn, "name": "Reader " + idn, "member_type": "student",
                                                        "temporary_password": PW})
    assert r.status_code == 200, r.text
    return idn, login(client, idn, PW)


def _title(client, cataloguer):
    r = client.post("/api/catalogue/titles", headers=cataloguer, json={"title": "Ebook Title " + uuid.uuid4().hex[:6], "copies": 1})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["copies"][0]["barcode"]


def _txt(text):
    return {"file": ("book.txt", text.encode(), "text/plain")}


def test_upload_pages_and_access_rules(client, head, desk, cataloguer):
    admin = login(client, "STF001")
    idn, student = _member(client, head)
    title_id, barcode = _title(client, cataloguer)

    assert client.get(f"/api/ebooks/{title_id}", headers=student).json() == {"title_id": title_id, "available": False, "page_count": 0, "can_read": False}
    assert client.post(f"/api/ebooks/{title_id}", headers=student, files=_txt("x")).status_code == 403
    assert client.post(f"/api/ebooks/{title_id}", headers=cataloguer, files=_txt("x")).status_code == 403
    assert client.post(f"/api/ebooks/{title_id}", headers=admin, files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 400

    r = client.post(f"/api/ebooks/{title_id}", headers=admin, files=_txt("Page one text\n---\nPage two text\n---\nPage three"))
    assert r.status_code == 200, r.text
    assert r.json()["page_count"] == 3 and r.json()["available"] is True

    # not borrowed yet: info says so and pages are refused
    info = client.get(f"/api/ebooks/{title_id}", headers=student).json()
    assert info["can_read"] is False and "Borrow" in info["reason"]
    assert client.get(f"/api/ebooks/{title_id}/pages/1", headers=student).status_code == 403
    # staff can preview
    assert client.get(f"/api/ebooks/{title_id}/pages/2", headers=head).json()["text"] == "Page two text"

    # after the desk issues the title, the member can read it
    assert client.post("/api/circulation/issue", headers=desk, json={"member_id_number": idn, "barcode": barcode}).status_code == 200
    page = client.get(f"/api/ebooks/{title_id}/pages/1", headers=student)
    assert page.status_code == 200 and page.json()["text"] == "Page one text" and page.json()["page_count"] == 3
    assert page.headers["cache-control"] == "no-store"
    assert client.get(f"/api/ebooks/{title_id}/pages/4", headers=student).status_code == 404
    assert client.get(f"/api/ebooks/{title_id}/pages/0", headers=student).status_code == 404

    # returning the book closes access again
    assert client.post("/api/circulation/return", headers=desk, json={"barcode": barcode, "condition": "ok"}).status_code == 200
    assert client.get(f"/api/ebooks/{title_id}/pages/1", headers=student).status_code == 403

    # replace, then remove
    assert client.post(f"/api/ebooks/{title_id}", headers=head, files=_txt("Only page")).json()["page_count"] == 1
    assert client.delete(f"/api/ebooks/{title_id}", headers=admin).json() == {"ok": True}
    assert client.get(f"/api/ebooks/{title_id}/pages/1", headers=head).status_code == 404


def test_sample_and_auto_pagination(client, cataloguer):
    admin = login(client, "STF001")
    title_id, _ = _title(client, cataloguer)
    r = client.post(f"/api/ebooks/{title_id}/sample", headers=admin)
    assert r.status_code == 200 and 3 <= r.json()["page_count"] <= 5

    long_text = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(12))
    r = client.post(f"/api/ebooks/{title_id}", headers=admin, files=_txt(long_text))
    assert r.json()["page_count"] > 1
    assert client.post(f"/api/ebooks/{title_id}", headers=admin, files=_txt("   \n ")).status_code == 400
    assert client.get("/api/ebooks/999999", headers=admin).status_code == 404
