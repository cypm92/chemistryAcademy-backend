from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_booking_management_column(bind=engine) -> None:
    """Add a nullable management state without changing legacy bookings."""
    inspector = inspect(bind)
    if not inspector.has_table("bookings"):
        return
    if "management_status" in {column["name"] for column in inspector.get_columns("bookings")}:
        return
    if bind.dialect.name == "postgresql":
        with bind.begin() as connection:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS management_status VARCHAR(20)"))
    else:
        with bind.begin() as connection:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN management_status VARCHAR(20)"))


def _upgrade_microsoft_schema(connection, dialect):
    inspector = inspect(connection)
    if inspector.has_table("teams_orphan_events"):
        columns = {column["name"] for column in inspector.get_columns("teams_orphan_events")}
        condition = " WHERE completed_at IS NULL" if "completed_at" in columns else ""
        if connection.execute(text("SELECT 1 FROM teams_orphan_events" + condition + " LIMIT 1")).first():
            raise RuntimeError("Legacy teams_orphan_events requires manual review before startup; preserve the table")
    # Standalone upgrades must create this even when no Teams tables exist.
    # No historical ownership can be safely inferred from legacy jobs.
    from .teams_models import TeamsEventOwnership, TeamsRemoteOperation
    TeamsEventOwnership.__table__.create(connection, checkfirst=True)
    # Rollout prerequisite: drain and stop all pre-upgrade workers before
    # deploying. An empty new table cannot prove no older PATCH is in flight;
    # historical remote operations cannot be reconstructed from legacy jobs.
    TeamsRemoteOperation.__table__.create(connection, checkfirst=True)
    inspector = inspect(connection)
    if inspector.has_table("teams_sync_jobs"):
        columns = {col["name"] for col in inspector.get_columns("teams_sync_jobs")}
        missing = {"id", "revision", "transaction_id"} - columns
        if missing:
            raise RuntimeError(
                "Teams legacy schema missing " + ", ".join(sorted(missing)) +
                "; explicit intervention required (transaction identities cannot be inferred)"
            )
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    additions = {
        # Legacy decision rows are deliberately not imported: review again.
        "teams_remote_operations": {
            "reviewed_at": timestamp_type,
            "reviewed_by_admin_id": "INTEGER",
            "reviewed_revision": "INTEGER",
            "reviewed_desired": "VARCHAR(10)",
        },
        "teams_creation_attempts": {
            "needs_review": "BOOLEAN NOT NULL DEFAULT FALSE",
            "authorized_at": timestamp_type,
            "dispatched_at": timestamp_type,
        },
        "microsoft_credentials": {
            "refresh_generation": "INTEGER NOT NULL DEFAULT 0",
            "refresh_lease_id": "VARCHAR(36)",
            "refresh_lease_expires_at": timestamp_type,
        },
        "teams_sync_jobs": {
            "creation_in_flight": "BOOLEAN NOT NULL DEFAULT FALSE",
            "reconcile_at": timestamp_type,
            "recreation_transaction_id": "VARCHAR(36)",
            "recreation_admin_id": "INTEGER",
            "recreation_at": timestamp_type,
            "recreation_reason": "VARCHAR(32)",
            "recreation_revision": "INTEGER",
            "recreation_tenant_id": "VARCHAR(36)",
            "recreation_account_id": "VARCHAR(36)",
            "recreation_prior_transaction_id": "VARCHAR(36)",
            "recreation_prior_event_id": "TEXT",
            "recreation_prior_join_url": "TEXT",
        },
    }
    optional = " IF NOT EXISTS" if dialect == "postgresql" else ""
    for table, definitions in additions.items():
        if not inspector.has_table(table):
            continue
        columns = {col["name"] for col in inspector.get_columns(table)}
        for name, definition in definitions.items():
            if name not in columns:
                connection.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN{optional} {name} {definition}"
                ))
                if (table == "teams_sync_jobs" and name == "creation_in_flight" and
                        {"event_id", "applied_revision", "revision", "last_error"} <= columns):
                    # Legacy pending jobs without an ID cannot prove that an
                    # earlier POST was not accepted. Fail closed on upgrade.
                    connection.execute(text(
                        "UPDATE teams_sync_jobs SET creation_in_flight = TRUE, "
                        "last_error = 'creation_result_unknown' WHERE event_id IS NULL "
                        "AND applied_revision < revision"
                    ))
    # Legacy decisions are historical evidence, never authorization for a POST.
    if inspector.has_table("teams_recreation_decisions") and inspector.has_table("teams_sync_jobs"):
        legacy = {c["name"] for c in inspector.get_columns("teams_recreation_decisions")}
        jobs = {c["name"] for c in inspector.get_columns("teams_sync_jobs")}
        if {"job_id", "replacement_transaction_id"} <= legacy and {"creation_in_flight", "last_error", "event_id"} <= jobs:
            connection.execute(text("UPDATE teams_sync_jobs SET creation_in_flight = TRUE, last_error = 'creation_result_unknown' WHERE event_id IS NULL AND EXISTS (SELECT 1 FROM teams_recreation_decisions d WHERE d.job_id = teams_sync_jobs.id AND d.replacement_transaction_id = teams_sync_jobs.transaction_id)"))
    if inspector.has_table("teams_sync_jobs"):
        if connection.execute(text(
            "SELECT 1 FROM teams_sync_jobs WHERE creation_in_flight = TRUE "
            "AND (transaction_id IS NULL OR TRIM(transaction_id) = '') LIMIT 1"
        )).first():
            raise RuntimeError("Teams in-flight transaction_id missing; explicit intervention required")
        # Create under the same migration lock even for standalone upgrades.
        from .teams_models import TeamsCreationAttempt
        TeamsCreationAttempt.__table__.create(connection, checkfirst=True)
        if connection.execute(text(
            "SELECT 1 FROM teams_sync_jobs j JOIN teams_creation_attempts a "
            "ON a.transaction_id = j.transaction_id WHERE j.creation_in_flight = TRUE "
            "AND a.job_id <> j.id LIMIT 1"
        )).first():
            raise RuntimeError("Teams transaction ID belongs to a different job")
        connection.execute(text(
            "INSERT INTO teams_creation_attempts "
            "(job_id, transaction_id, revision, state) "
            "SELECT j.id, j.transaction_id, j.revision, 'unknown' FROM teams_sync_jobs j "
            "WHERE j.creation_in_flight = TRUE AND NOT EXISTS "
            "(SELECT 1 FROM teams_creation_attempts a WHERE a.transaction_id = j.transaction_id)"
        ))
    table = "microsoft_oauth_generation"
    if inspector.has_table(table):
        columns = {col["name"] for col in inspector.get_columns(table)}
        for name in ("account_object_id", "tenant_id"):
            if name not in columns:
                optional = " IF NOT EXISTS" if dialect == "postgresql" else ""
                connection.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN{optional} {name} VARCHAR(36)"
                ))
        if inspector.has_table("microsoft_credentials"):
            credential = connection.execute(text(
                "SELECT 1 FROM microsoft_credentials LIMIT 1"
            )).first()
            binding = connection.execute(text(
                "SELECT account_object_id, tenant_id FROM microsoft_oauth_generation WHERE id = 1"
            )).first()
            if credential and (binding is None or not binding[0] or not binding[1]):
                raise RuntimeError(
                    "Microsoft credential has no complete owner binding; explicit intervention required"
                )


def ensure_application_schema(bind=engine) -> None:
    """Serialize metadata creation and Microsoft migration in one transaction."""
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database for Microsoft schema migration")
    with bind.connect() as connection:
        if dialect == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.execute(text("SELECT pg_advisory_xact_lock(7415928163042)"))
        try:
            Base.metadata.create_all(connection)
            _upgrade_microsoft_schema(connection, dialect)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


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
