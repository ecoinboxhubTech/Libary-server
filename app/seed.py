"""Illustrative demo data so the prototype opens with something to explore.
Titles are real, widely used transport and logistics textbooks; editions, years, shelf marks and prices are
placeholders. Replace everything through the import tool (Catalogue > Import) before real use."""
from datetime import timedelta

from sqlalchemy import func, select

from .config import get_config
from .database import SessionLocal
from .models import Copy, Hold, Member, Title
from .security import hash_password
from .services import circulation as circ
from .timeutil import utcnow

PEOPLE = [
    # id_number, name, member_type, role, programme, level, department, email
    ("STF001", "Chidi Okeke", "non_academic_staff", "admin", None, None, "ICT Unit", "chidi@example.com"),
    ("STF002", "Dr. Musa Ibrahim", "academic_staff", "head_librarian", None, None, "Library", "musa@example.com"),
    ("STF003", "Mrs. Hadiza Yusuf", "non_academic_staff", "circulation", None, None, "Library", "hadiza@example.com"),
    ("STF004", "Zainab Lawal", "non_academic_staff", "cataloguer", None, None, "Library", "zainab@example.com"),
    ("STF005", "Ngozi Eze", "non_academic_staff", "finance", None, None, "Bursary", "ngozi@example.com"),
    ("STF010", "Engr. Bello Garba", "academic_staff", "lecturer", None, None, "Maritime Studies", "bello@example.com"),
    ("NITT/HND/24/001", "Amina Sani", "student", "student", "HND Transport Management", "HND II", "Transport Management", "amina@example.com"),
    ("NITT/HND/24/002", "Emeka Nwosu", "student", "student", "HND Logistics and Supply Chain", "HND I", "Logistics and Supply Chain", "emeka@example.com"),
    ("NITT/ND/25/014", "Fatima Abdullahi", "student", "student", "ND Maritime Transport", "ND II", "Maritime Studies", "fatima@example.com"),
    ("VIS001", "Hajiya Rakiya Danladi", "visitor", "student", None, None, None, "rakiya@example.com"),
]

# title, authors, subject_area, department, format, call_number, subjects, year, copies, shelf, collection, cost
TITLES = [
    ("Maritime Economics", "Martin Stopford", "maritime", "Maritime Studies", "print_book", "HE571", "Shipping; Maritime economics", 2009, 3, "Section A / Shelf 2", "general", 45000),
    ("Ship Stability for Masters and Mates", "Bryan Barrass; D. R. Derrett", "maritime", "Maritime Studies", "print_book", "VM159", "Ship stability; Naval architecture", 2012, 2, "Section A / Shelf 4", "general", 38000),
    ("Port Economics, Management and Policy", "Theo Notteboom; Athanasios Pallis; Jean-Paul Rodrigue", "maritime", "Maritime Studies", "print_book", "HE551", "Ports; Terminal management", 2022, 1, "Section A / Shelf 5", "general", 60000),
    ("Supply Chain Management: Strategy, Planning, and Operation", "Sunil Chopra; Peter Meindl", "logistics", "Logistics and Supply Chain", "print_book", "HD38.5", "Supply chain; Logistics", 2016, 4, "Section B / Shelf 1", "general", 42000),
    ("Logistics and Supply Chain Management", "Martin Christopher", "logistics", "Logistics and Supply Chain", "print_book", "HD38.5", "Logistics; Supply chain", 2016, 3, "Section B / Shelf 1", "general", 40000),
    ("Transportation: A Global Supply Chain Perspective", "John J. Coyle; Robert A. Novack; Brian Gibson; Edward J. Bardi", "logistics", "Transport Management", "print_book", "HE151", "Transport management; Freight", 2011, 2, "Section B / Shelf 3", "general", 41000),
    ("Introduction to Transportation Engineering", "James H. Banks", "road", "Transport Management", "print_book", "TA1145", "Transport engineering; Highways", 2002, 2, "Section C / Shelf 2", "general", 36000),
    ("Principles of Highway Engineering and Traffic Analysis", "Fred L. Mannering; Scott S. Washburn", "road", "Transport Management", "print_book", "TE145", "Highway design; Traffic analysis", 2012, 2, "Section C / Shelf 2", "general", 39000),
    ("Traffic Engineering", "Roger P. Roess; Elena S. Prassas; William R. McShane", "road", "Transport Management", "print_book", "HE333", "Traffic engineering", 2010, 1, "Section C / Shelf 3", "reserve", 44000),
    ("Introduction to Air Transport Economics: From Theory to Applications", "Bijan Vasigh; Ken Fleming; Thomas Tacker", "aviation", "Aviation Studies", "print_book", "HE9776", "Air transport; Airline economics", 2013, 2, "Section D / Shelf 1", "general", 43000),
    ("Modern Railway Track", "Coenraad Esveld", "rail", "Transport Management", "print_book", "TF200", "Railway engineering; Track", 2001, 1, "Section D / Shelf 4", "reference_only", 70000),
    ("Transport Policy Handbook (sample record)", "Library sample", "policy", "Transport Management", "standard", "HE193", "Transport policy; Regulation", 2020, 1, "Section E / Shelf 1", "reference_only", None),
]


# code suffix, name, type, email, contact, status, note, demo live key (approved/suspended only)
INSTITUTIONS = [
    ("Zaria City College of Technology", "college", "registrar@zcct.example.com", "Mr. Sani Abubakar", "approved",
     "Verified by library board", "nitt_live_demo_zcct_0000000000000000000000"),
    ("Lagos Maritime Training Academy", "training_centre", "admin@lmta.example.com", "Capt. Funke Adeyemi", "approved",
     "MoU on file", "nitt_live_demo_lmta_0000000000000000000000"),
    ("Northern Aviation Institute", "polytechnic", "info@nai.example.com", "Dr. Halima Garba", "pending", None, None),
    ("Riverside Business School", "college", "office@rbs.example.com", "Mr. Tunde Bakare", "rejected",
     "Could not verify institution details", None),
    ("Sahel Logistics College", "college", "bursar@slc.example.com", "Mrs. Ngozi Eze", "suspended",
     "Agreement under review", "nitt_live_demo_slc_00000000000000000000000"),
]
STUDENTS = {  # institution index -> (id_number, name, programme, level, department)
    0: [("ZCCT/24/001", "Ibrahim Yakubu", "ND Transport Management", "ND I", "Transport Management"),
        ("ZCCT/24/002", "Maryam Aliyu", "ND Logistics", "ND I", "Logistics and Supply Chain"),
        ("ZCCT/23/010", "Samuel Dauda", "HND Transport Management", "HND I", "Transport Management")],
    1: [("LMTA/25/001", "Kelechi Obi", "Certificate in Marine Operations", "Cert I", "Maritime Studies"),
        ("LMTA/25/002", "Blessing Udoh", "Certificate in Port Management", "Cert I", "Maritime Studies")],
    4: [("SLC/24/001", "Usman Kabir", "ND Logistics", "ND II", "Logistics and Supply Chain")],
}


def seed_institutions() -> None:
    """Demo partner institutions in every status, with students and a loan. Safe to call repeatedly."""
    from .models import Institution
    from .services.institutions import hash_key
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Institution)):
            return
        reviewer = db.scalar(select(Member).where(Member.id_number == "STF002"))
        officer = db.scalar(select(Member).where(Member.id_number == "STF003"))
        now = utcnow()
        pw = hash_password(get_config().demo_password)
        insts = []
        for i, (name, typ, email, contact, status, note, live_key) in enumerate(INSTITUTIONS):
            inst = Institution(code=f"TMP{i}", name=name, type=typ, email=email, contact_name=contact,
                               phone=f"0803000{i:04d}", address="Kaduna State, Nigeria", status=status, status_note=note,
                               max_students=200, created_at=now - timedelta(days=30 - i * 5),
                               reviewed_by_id=reviewer.id if reviewer and status != "pending" else None,
                               reviewed_at=now - timedelta(days=20 - i) if status != "pending" else None)
            db.add(inst)
            db.flush()
            inst.code = f"INST{inst.id:04d}"
            inst.sandbox_key_hash = hash_key(f"nitt_test_demo_{inst.code.lower()}")
            if live_key:
                inst.live_key_hash, inst.live_key_prefix = hash_key(live_key), live_key[:14]
            insts.append(inst)
        db.flush()
        students = {}
        for idx, rows in STUDENTS.items():
            for idn, name, prog, level, dept in rows:
                m = Member(id_number=idn, name=name, member_type="student", role="student", programme=prog, level=level,
                           department=dept, email=f"{idn.lower().replace('/', '.')}@example.com", password_hash=pw,
                           consent_at=now, institution_id=insts[idx].id)
                db.add(m)
                students[idn] = m
        db.flush()
        copy = db.scalar(select(Copy).join(Title, Title.id == Copy.title_id)
                         .where(Title.title == "Supply Chain Management: Strategy, Planning, and Operation",
                                Copy.status == "available").order_by(Copy.id))
        if copy and officer:
            circ.issue(db, students["ZCCT/24/002"], copy, officer, now=now - timedelta(days=4))
        db.commit()


def seed_approvals() -> None:
    """Demo requests in every state so the approval screens have something to show. Safe to call repeatedly."""
    from .models import LedgerEntry, Loan, LoanRequest, PaymentRequest, ReturnRequest
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(LoanRequest)):
            return
        by_id = {m.id_number: m for m in db.scalars(select(Member))}
        title = {t.title: t for t in db.scalars(select(Title))}
        head, now = by_id["STF002"], utcnow()

        def open_loan(idn: str) -> Loan | None:
            return db.scalar(select(Loan).where(Loan.member_id == by_id[idn].id, Loan.returned_at.is_(None)).order_by(Loan.id))

        approved_loan = open_loan("NITT/ND/25/014")
        db.add_all([
            LoanRequest(member_id=by_id["NITT/ND/25/014"].id, title_id=title["Introduction to Transportation Engineering"].id,
                        note="Needed for my project", created_at=now - timedelta(hours=5)),
            LoanRequest(member_id=by_id["ZCCT/24/001"].id,
                        title_id=title["Principles of Highway Engineering and Traffic Analysis"].id,
                        created_at=now - timedelta(hours=2)),
            LoanRequest(member_id=by_id["STF010"].id, title_id=title["Ship Stability for Masters and Mates"].id,
                        note="Course material for ND II", created_at=now - timedelta(hours=1)),
            LoanRequest(member_id=by_id["NITT/ND/25/014"].id, title_id=title["Port Economics, Management and Policy"].id,
                        status="approved", loan_id=approved_loan.id if approved_loan else None, decided_by_id=head.id,
                        decided_at=now - timedelta(days=2), created_at=now - timedelta(days=2, hours=1)),
            LoanRequest(member_id=by_id["NITT/HND/24/001"].id, title_id=title["Maritime Economics"].id, status="rejected",
                        decision_note="Please return your current item first", decided_by_id=head.id,
                        decided_at=now - timedelta(days=5), created_at=now - timedelta(days=5, hours=2)),
        ])
        overdue = open_loan("NITT/HND/24/002")
        current = open_loan("NITT/HND/24/001")
        if overdue:
            db.add(ReturnRequest(loan_id=overdue.id, member_id=overdue.member_id, condition="ok",
                                 note="Dropped at the desk this morning", created_at=now - timedelta(hours=3)))
        if current:
            db.add(ReturnRequest(loan_id=current.id, member_id=current.member_id, condition="damaged", status="rejected",
                                 note="Cover torn", decision_note="Book has not been received at the desk",
                                 decided_by_id=head.id, decided_at=now - timedelta(days=1), created_at=now - timedelta(days=1, hours=3)))
        owed = max(0, circ.member_balance(db, by_id["NITT/HND/24/002"].id))
        if owed >= 100:
            db.add(PaymentRequest(member_id=by_id["NITT/HND/24/002"].id, amount=min(owed, 200), method="bank_transfer",
                                  receipt_no="TRF-DEMO-0001", note="Paid via GTBank transfer", created_at=now - timedelta(hours=4)))
        db.add(PaymentRequest(member_id=by_id["NITT/HND/24/001"].id, amount=500, method="cash", receipt_no="CASH-DEMO-0002",
                              status="rejected", decision_note="Receipt number not found", decided_by_id=head.id,
                              decided_at=now - timedelta(days=3), created_at=now - timedelta(days=3, hours=1)))
        db.commit()


EBOOK_DEMO_TITLES = ["Maritime Economics", "Logistics and Supply Chain Management", "Introduction to Transportation Engineering"]


def seed_ebooks() -> None:
    """A short placeholder e-copy (4 pages) on a few demo titles, so the reader works out of the box. Safe to repeat."""
    from .models import Ebook
    from .services import ebooks as ebook_service
    with SessionLocal() as db:
        for name in EBOOK_DEMO_TITLES:
            t = db.scalar(select(Title).where(Title.title == name))
            if not t or db.scalar(select(Ebook.id).where(Ebook.title_id == t.id)):
                continue
            pages = ebook_service.sample_pages(t.title, t.authors, t.subject_area)
            db.add(Ebook(title_id=t.id, pages=pages, page_count=len(pages), filename="sample.txt"))
        db.commit()


def seed_demo() -> None:
    cfg = get_config()
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Title)):
            return  # already seeded
        pw = hash_password(cfg.demo_password)
        now = utcnow()
        members = {}
        for idn, name, mtype, role, prog, level, dept, email in PEOPLE:
            m = Member(id_number=idn, name=name, member_type=mtype, role=role, programme=prog, level=level,
                       department=dept, email=email, password_hash=pw, must_change_password=False, consent_at=now)
            db.add(m)
            members[idn] = m
        db.flush()

        titles: dict[str, Title] = {}
        for i, (title, authors, area, dept, fmt, call, subjects, year, n, shelf, coll, cost) in enumerate(TITLES):
            t = Title(title=title, authors=authors, subject_area=area, department=dept, format=fmt, call_number=call,
                      subjects=subjects, year=year, created_at=now - timedelta(days=len(TITLES) - i))
            t.refresh_search_text()
            db.add(t)
            db.flush()
            for _ in range(n):
                c = Copy(title_id=t.id, barcode="PENDING", shelf_location=shelf, collection=coll, cost=cost)
                db.add(c)
                db.flush()
                c.barcode = f"NITT{c.id:07d}"
            titles[title] = t
        db.flush()

        officer = members["STF003"]

        def first_free(title: str) -> Copy:
            return db.scalar(select(Copy).where(Copy.title_id == titles[title].id, Copy.status == "available").order_by(Copy.id))

        # An overdue loan (fine accrues), a current loan, and a title that is fully out with a waiting hold
        circ.issue(db, members["NITT/HND/24/002"], first_free("Logistics and Supply Chain Management"), officer, now=now - timedelta(days=24))
        circ.issue(db, members["NITT/HND/24/001"], first_free("Maritime Economics"), officer, now=now - timedelta(days=3))
        circ.issue(db, members["NITT/ND/25/014"], first_free("Port Economics, Management and Policy"), officer, now=now - timedelta(days=2))
        db.flush()
        db.add(Hold(title_id=titles["Port Economics, Management and Policy"].id, member_id=members["NITT/HND/24/001"].id,
                    status="queued", queued_at=now - timedelta(days=1)))
        circ.accrue_fines(db, now)
        db.commit()
