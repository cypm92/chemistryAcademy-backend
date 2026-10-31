from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_notification_lease_column() -> None:
    """Add the lease column to an existing notification outbox table."""
    table = "notification_events"
    if not inspect(engine).has_table(table):
        return
    columns = {column["name"] for column in inspect(engine).get_columns(table)}
    if "lease_expires_at" not in columns:
        column_type = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME"
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN lease_expires_at {column_type}"))


def ensure_public_resource_columns(bind=engine) -> None:
    """Evolve legacy public_resources tables without requiring a DB-specific migration tool."""
    table = "public_resources"
    if not inspect(bind).has_table(table):
        return
    columns = {column["name"] for column in inspect(bind).get_columns(table)}
    definitions = {
        "category": "VARCHAR(10) DEFAULT 'form'",
        "community": "VARCHAR(80)",
        "exam_year": "INTEGER",
        "exam_part": "VARCHAR(12)",
    }
    with bind.begin() as connection:
        for name, definition in definitions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        connection.execute(text(f"UPDATE {table} SET category = 'form' WHERE kind = 'document' AND (category IS NULL OR category = '')"))
        connection.execute(text(f"UPDATE {table} SET category = NULL WHERE kind = 'video'"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
