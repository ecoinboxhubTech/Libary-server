import uuid

import pytest

from .conftest import login
from .test_v1_checkout import fresh_member, slug_of
from .test_v1_library import V1, issue, make_title

PAGES = "Page one text\n---\nPage two text\n---\nPage three text"


def txt(text, name="book.txt"):
    return {"file": (name, text.encode(), "text/plain")}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "STF001")


def test_upload_is_admin_or_head_librarian_only_and_text_only(client, admin, head, cataloguer, student):
    slug = slug_of(client, t := make_title(client, cataloguer))
    url = f"{V1}/titles/{slug}/ebook"
    assert client.post(url, files=txt("x")).status_code == 401
    assert client.post(url, headers=student, files=txt("x")).status_code == 403
    assert client.post(url, headers=cataloguer, files=txt("x")).status_code == 403
    bad = client.post(url, headers=admin, files=txt("x", "book.pdf"))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "BAD_REQUEST"
    assert client.post(url, headers=admin, files=txt("   \n ")).status_code == 400
    ok = client.post(url, headers=admin, files=txt(PAGES))
    assert ok.status_code == 200 and ok.json()["pageCount"] == 3 and ok.json()["titleId"] == slug
    assert client.post(url, headers=head, files=txt("Only page")).json()["pageCount"] == 1  # replaces
    assert client.delete(url, headers=student).status_code == 403
    assert client.delete(url, headers=admin).json() == {"ok": True}
    assert client.get(url, headers=student).json()["available"] is False


def test_members_read_only_while_the_title_is_on_loan(client, admin, desk, head, cataloguer):
    idn, h = fresh_member(client, head)
    t = make_title(client, cataloguer)
    slug = slug_of(client, t)
    base = f"{V1}/titles/{slug}/ebook"
    assert client.get(base, headers=h).json()["available"] is False
    client.post(base, headers=admin, files=txt(PAGES))

    card = client.get(f"{V1}/titles/{slug}").json()
    assert card["hasEbook"] is True and card["ebookPages"] == 3
    assert all("barcode" not in c for c in card["copies"])
    listed = client.get(f"{V1}/titles", params={"q": t["title"]}).json()["data"]
    assert next(x for x in listed if x["id"] == slug)["hasEbook"] is True

    info = client.get(base, headers=h).json()
    assert info["available"] and info["pageCount"] == 3 and info["canRead"] is False
    assert info["restrictions"] == {"download": False, "print": False}
    locked = client.get(f"{base}/pages/1", headers=h)  # not borrowed yet
    assert locked.status_code == 403 and locked.json()["error"]["code"] == "FORBIDDEN"
    assert client.get(f"{base}/pages/1").status_code == 401

    out = client.post(f"{V1}/checkouts", headers=h, json={"items": [slug]}).json()
    assert out["status"] == "confirmed"
    loan = client.get(f"{V1}/me/loans", headers=h).json()["data"][0]
    assert loan["canRead"] is True and loan["readUrl"] == base and "barcode" not in loan

    assert client.get(base, headers=h).json()["canRead"] is True
    p2 = client.get(f"{base}/pages/2", headers=h)
    assert p2.status_code == 200 and p2.json()["text"] == "Page two text" and p2.json()["pageCount"] == 3
    assert p2.headers["cache-control"] == "no-store" and p2.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in p2.headers  # nothing that asks the browser to save a file
    assert client.get(f"{base}/pages/4", headers=h).status_code == 404
    assert client.get(f"{base}/pages/0", headers=h).status_code == 404

    done = client.post(f"{V1}/loans/{loan['id']}/return", headers=desk)  # giving the book back ends access
    assert done.status_code == 200 and done.json()["loan"]["canRead"] is False
    assert client.get(f"{base}/pages/1", headers=h).status_code == 403
    assert client.get(base, headers=h).json()["canRead"] is False


def test_staff_can_preview_and_sample_book_has_four_pages(client, admin, head, cataloguer):
    slug = slug_of(client, make_title(client, cataloguer, title=f"Sample Subject {uuid.uuid4().hex[:5]}"))
    base = f"{V1}/titles/{slug}/ebook"
    s = client.post(f"{base}/sample", headers=admin)
    assert s.status_code == 200 and s.json()["pageCount"] == 4
    assert client.post(f"{base}/sample", headers=cataloguer).status_code == 403
    first = client.get(f"{base}/pages/1", headers=head).json()
    assert first["page"] == 1 and "Sample e-copy" in first["text"]


def test_no_endpoint_returns_a_file(client):
    """Every e-book endpoint answers in JSON. There is no route that serves an upload, so there is nothing to save."""
    spec = client.get("/openapi.json").json()["paths"]
    seen = 0
    for path, ops in spec.items():
        if "ebook" not in path:
            continue
        for method, op in ops.items():
            if method != "get":
                continue
            seen += 1
            for code, resp in op["responses"].items():
                if code.startswith("2"):
                    assert set(resp.get("content", {"application/json": 1})) == {"application/json"}, path
    assert seen >= 4


def test_demo_titles_ship_with_a_four_page_sample_ebook(client):
    d = client.get(f"{V1}/titles/maritime-economics").json()
    assert d["hasEbook"] is True and d["ebookPages"] == 4
