"""Typo-tolerant catalogue search (SRCH-1..4). Uses Pinecone for semantic search when configured;
falls back to RapidFuzz fuzzy matching otherwise."""
from rapidfuzz import fuzz, process
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from ..models import Copy, SearchLog, Title
from ..serializers import availability_map, title_out
from . import vector_store

FUZZY_CUTOFF = 68
SORTS = ("title", "year_desc", "year_asc", "newest")  # anything else keeps relevance order


def _filtered_ids_stmt(f: dict):
    stmt = select(Title.id, Title.search_text).where(Title.retired.is_(False))
    if f.get("subject_area"):
        stmt = stmt.where(Title.subject_area == f["subject_area"])
    if f.get("department"):
        stmt = stmt.where(Title.department == f["department"])
    if f.get("format"):
        stmt = stmt.where(Title.format == f["format"])
    if f.get("language"):
        stmt = stmt.where(Title.language == f["language"])
    if f.get("year"):
        stmt = stmt.where(Title.year == f["year"])
    if f.get("available_only"):
        stmt = stmt.where(or_(Title.is_digital.is_(True),
                              exists().where(Copy.title_id == Title.id, Copy.status == "available",
                                             Copy.collection != "reference_only")))
    return stmt


def search_titles(db: Session, q: str | None, filters: dict, page: int, page_size: int,
                  member_id: int | None = None, sort: str | None = None) -> dict:
    q = (q or "").strip().lower()

    if q and vector_store.is_configured():
        ids = vector_store.semantic_search(q, filters, top_k=200)
        if filters.get("available_only") and ids:
            avail_set = set(db.scalars(
                select(Copy.title_id).where(
                    Copy.title_id.in_(ids),
                    Copy.status == "available",
                    Copy.collection != "reference_only",
                )
            )) | set(db.scalars(select(Title.id).where(Title.id.in_(ids), Title.is_digital.is_(True))))
            ids = [i for i in ids if i in avail_set]
    elif q:
        stmt = _filtered_ids_stmt(filters)
        rows = db.execute(stmt).all()
        texts = {tid: text for tid, text in rows}
        hits = process.extract(q, texts, scorer=fuzz.WRatio, score_cutoff=FUZZY_CUTOFF, limit=300)
        scores = {tid: score for _, score, tid in hits}
        for tid, text in texts.items():  # a short query ("port") scores low against a long record; still include it
            if q in text:
                scores.setdefault(tid, 70)
        ranked = []
        for tid, score in scores.items():
            bonus = 20 if q in texts[tid] else 0  # exact phrase / ISBN / call-number matches rise to the top
            ranked.append((score + bonus, tid))
        ranked.sort(key=lambda x: -x[0])
        ids = [tid for _, tid in ranked]
    else:
        stmt = _filtered_ids_stmt(filters)
        ids = [tid for (tid, _) in db.execute(stmt.order_by(Title.title)).all()]

    if sort in SORTS and ids:
        meta = {r.id: r for r in db.execute(select(Title.id, Title.title, Title.year, Title.created_at)
                                            .where(Title.id.in_(ids)))}
        keys = {"title": lambda i: meta[i].title.lower(),
                "year_desc": lambda i: -(meta[i].year or 0), "year_asc": lambda i: meta[i].year or 9999,
                "newest": lambda i: -meta[i].created_at.timestamp()}
        ids = sorted(ids, key=keys[sort])  # stable, so relevance order breaks ties

    total = len(ids)
    if q and page == 1:  # SRCH-5: keep a record of searches, especially empty ones
        db.add(SearchLog(query=q[:300], result_count=total, member_id=member_id))
        db.commit()

    page_ids = ids[(page - 1) * page_size: page * page_size]
    titles = {t.id: t for t in db.scalars(select(Title).where(Title.id.in_(page_ids)))}
    avail = availability_map(db, page_ids)
    return {
        "total": total, "page": page, "page_size": page_size,
        "results": [title_out(titles[i], avail[i]) for i in page_ids if i in titles],
    }


def facets(db: Session) -> dict:
    def distinct(col):
        return [v for (v,) in db.execute(select(col).where(Title.retired.is_(False), col.isnot(None))
                                         .group_by(col).order_by(col)).all() if v]
    return {"departments": distinct(Title.department), "languages": distinct(Title.language),
            "formats": distinct(Title.format), "subject_areas": distinct(Title.subject_area)}
