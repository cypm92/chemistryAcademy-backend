from sqlalchemy import inspect
from ephemeral_postgres import postgres_test_db

from app.database import Base
import app.teams_models  # register models


def test_recreation_authorization_is_on_job_not_separate_table(postgres_test_db):
    engine = postgres_test_db.engine
    assert engine.dialect.name == 'postgresql'
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert 'teams_recreation_decisions' not in inspector.get_table_names()
    columns = {c['name'] for c in inspector.get_columns('teams_sync_jobs')}
    assert {'recreation_transaction_id', 'recreation_admin_id', 'recreation_at', 'recreation_revision',
            'recreation_reason', 'recreation_tenant_id', 'recreation_account_id'} <= columns


def test_legacy_authorization_is_preserved_but_never_imported(postgres_test_db):
    from sqlalchemy import text
    from app.database import ensure_application_schema
    engine = postgres_test_db.engine
    with engine.begin() as db:
        db.execute(text("CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, transaction_id VARCHAR(36) NOT NULL, event_id TEXT, creation_in_flight BOOLEAN NOT NULL, last_error VARCHAR(200))"))
        db.execute(text("CREATE TABLE teams_recreation_decisions (id INTEGER PRIMARY KEY, job_id INTEGER, replacement_transaction_id VARCHAR(36))"))
        db.execute(text("INSERT INTO teams_sync_jobs VALUES (1, 2, 'replacement', NULL, FALSE, NULL)"))
        db.execute(text("INSERT INTO teams_recreation_decisions VALUES (1, 1, 'replacement')"))
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with engine.connect() as db:
        assert db.execute(text("SELECT creation_in_flight, last_error FROM teams_sync_jobs")).one() == (True, 'creation_result_unknown')
        assert db.execute(text("SELECT state, authorized_at FROM teams_creation_attempts")).one() == ('unknown', None)
        assert db.execute(text("SELECT COUNT(*) FROM teams_recreation_decisions")).scalar_one() == 1
