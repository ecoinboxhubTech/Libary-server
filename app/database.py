from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_config

cfg = get_config()
is_sqlite = cfg.db_url.startswith("sqlite")

engine = create_engine(
    cfg.db_url,
    connect_args={"check_same_thread": False} if is_sqlite else {},
    pool_pre_ping=True,
)

if is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # survives power cuts better than the default
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# (table, column, DDL type) added after the first deployment. create_all() never alters existing tables.
NEW_COLUMNS = [
    ("members", "institution_id", "INTEGER REFERENCES institutions(id)"),
    ("titles", "slug", "VARCHAR(120)"),
    ("titles", "topics", "JSON"),
    ("titles", "is_digital", "BOOLEAN DEFAULT FALSE"),
    ("titles", "repository_path", "VARCHAR(300)"),
    ("titles", "quote", "VARCHAR(400)"),
    ("titles", "course", "VARCHAR(80)"),
    ("titles", "lecturer", "VARCHAR(120)"),
    ("titles", "pages", "INTEGER"),
    ("payments", "authorization_url", "VARCHAR(300)"),
    ("webhook_events", "next_attempt_at", "TIMESTAMP"),
    ("loans", "collection_point", "VARCHAR(30)"),
    ("loans", "checkout_id", "VARCHAR(20) REFERENCES checkouts(id)"),
]
NEW_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_titles_slug ON titles (slug)",
    "CREATE INDEX IF NOT EXISTS ix_members_institution_id ON members (institution_id)",
    "CREATE INDEX IF NOT EXISTS ix_loans_checkout_id ON loans (checkout_id)",
]


def ensure_columns() -> None:
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in NEW_COLUMNS:
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        for stmt in NEW_INDEXES:
            conn.execute(text(stmt))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
