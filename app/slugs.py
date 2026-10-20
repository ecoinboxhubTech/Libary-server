"""Public slugs for titles, e.g. "principles-of-maritime-logistics". The /api/v1 front end addresses titles by slug."""
import re

from sqlalchemy import select


def slugify(text: str, maxlen: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:maxlen].strip("-")
    return slug or "title"


def unique_slug(conn, table, text: str, own_id: int | None) -> str:
    """`conn` is a Connection or a Session; both can execute a Core select."""
    base = slugify(text)
    slug, n = base, 1
    while True:
        taken = conn.execute(select(table.c.id).where(table.c.slug == slug)).first()
        if not taken or taken[0] == own_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


def backfill_slugs(db) -> int:
    """Titles created before slugs existed get one. Safe to run on every start."""
    from .models import Title
    rows = db.scalars(select(Title).where(Title.slug.is_(None)).order_by(Title.id)).all()
    for t in rows:
        t.slug = unique_slug(db, Title.__table__, t.title, t.id)
        db.flush()
    db.commit()
    return len(rows)
