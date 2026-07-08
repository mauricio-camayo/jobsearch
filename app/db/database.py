import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/jobsearch.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def _rebuild_table_dropping_unique(conn, table: str, create_sql: str, copy_columns: str) -> None:
    """Rebuild `table` without its old single-tenant UNIQUE constraint, preserving data.

    Pre-multi-tenant installs created scoring_rubric.dimension and
    company_blacklist.company_name as globally UNIQUE; now that these are
    per-user, that constraint must be dropped. SQLite has no ALTER TABLE DROP
    CONSTRAINT, so this renames the old table, recreates it from the current
    model shape, copies rows across (user_id defaults to NULL, backfilled by
    seed_legacy_user_and_backfill), and drops the renamed original.
    No-op on fresh installs or installs already migrated (constraint absent).
    """
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).fetchone()
    if row is None or row[0] is None or "UNIQUE" not in row[0].upper():
        return
    tmp = f"{table}_pre_multiuser"
    conn.execute(text(f"ALTER TABLE {table} RENAME TO {tmp}"))
    conn.execute(text(create_sql))
    conn.execute(text(f"INSERT INTO {table} ({copy_columns}) SELECT {copy_columns} FROM {tmp}"))
    conn.execute(text(f"DROP TABLE {tmp}"))


def apply_migrations() -> None:
    """Add columns that were introduced after the initial create_all.

    SQLite does not support ADD COLUMN IF NOT EXISTS, so we catch the
    duplicate-column OperationalError and continue silently.
    """
    with engine.connect() as conn:
        try:
            _rebuild_table_dropping_unique(
                conn,
                "scoring_rubric",
                """
                CREATE TABLE scoring_rubric (
                    id INTEGER NOT NULL,
                    user_id INTEGER,
                    dimension VARCHAR NOT NULL,
                    weight INTEGER NOT NULL,
                    is_bonus BOOLEAN NOT NULL,
                    PRIMARY KEY (id)
                )
                """,
                "id, dimension, weight, is_bonus",
            )
            conn.commit()
        except Exception:
            conn.rollback()

        try:
            _rebuild_table_dropping_unique(
                conn,
                "company_blacklist",
                """
                CREATE TABLE company_blacklist (
                    id INTEGER NOT NULL,
                    user_id INTEGER,
                    company_name VARCHAR NOT NULL,
                    notes TEXT,
                    created_at DATETIME NOT NULL,
                    PRIMARY KEY (id)
                )
                """,
                "id, company_name, notes, created_at",
            )
            conn.commit()
        except Exception:
            conn.rollback()

    migrations = [
        "ALTER TABLE tracker_records ADD COLUMN fit_breakdown TEXT",
        "ALTER TABLE tracker_records ADD COLUMN quality_flags TEXT",
        "ALTER TABLE app_config ADD COLUMN shown_expiry_days INTEGER NOT NULL DEFAULT 30",
        "ALTER TABLE user_profiles ADD COLUMN user_id INTEGER",
        "ALTER TABLE tracker_records ADD COLUMN user_id INTEGER",
        "ALTER TABLE job_listings ADD COLUMN user_id INTEGER",
        "ALTER TABLE company_blacklist ADD COLUMN user_id INTEGER",
        "ALTER TABLE app_config ADD COLUMN user_id INTEGER",
        "ALTER TABLE search_params ADD COLUMN user_id INTEGER",
        "ALTER TABLE scoring_rubric ADD COLUMN user_id INTEGER",
        "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE search_engines ADD COLUMN search_params JSON NOT NULL DEFAULT '{}'",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                # Column already exists — safe to ignore
                pass

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
