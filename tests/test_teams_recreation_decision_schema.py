from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import ensure_application_schema
from test_teams_creation_attempt_schema import attempt_engine



def create_legacy_decisions(conn):
    pk = "SERIAL" if conn.dialect.name == "postgresql" else "INTEGER"
    conn.execute(text(f"""CREATE TABLE teams_recreation_decisions (
        id {pk} PRIMARY KEY, job_id INTEGER NOT NULL, requested_by_admin_id INTEGER NOT NULL,
        expected_revision INTEGER NOT NULL, replacement_transaction_id VARCHAR(36) NOT NULL UNIQUE,
        prior_transaction_id VARCHAR(36) NOT NULL, reason VARCHAR(32) NOT NULL,
        prior_event_id TEXT, prior_join_url TEXT, accepted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(job_id, expected_revision))"""))

def test_legacy_migration_preserves_explicit_decisions_without_inference(attempt_engine):
    engine = attempt_engine
    old = str(uuid4())
    with engine.begin() as conn:
        create_legacy_decisions(conn)
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, transaction_id VARCHAR(36) NOT NULL, revision INTEGER NOT NULL, creation_in_flight BOOLEAN NOT NULL)'))
        conn.execute(text('INSERT INTO teams_sync_jobs VALUES (1, :txn, 4, FALSE)'), {'txn': old})
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM teams_recreation_decisions')).scalar_one() == 0
        conn.execute(text('INSERT INTO teams_recreation_decisions (job_id, requested_by_admin_id, expected_revision, replacement_transaction_id, prior_transaction_id, reason, prior_event_id, prior_join_url) VALUES (1, 7, 4, :new, :old, :reason, NULL, NULL)'), {'new': str(uuid4()), 'old': old, 'reason': 'creation_result_unknown'})
        conn.execute(text('DELETE FROM teams_sync_jobs WHERE id = 1'))
    ensure_application_schema(engine)
    with engine.connect() as conn:
        row = conn.execute(text('SELECT job_id, requested_by_admin_id, expected_revision, prior_transaction_id, reason, prior_event_id, prior_join_url, accepted_at FROM teams_recreation_decisions')).one()
        assert row[:7] == (1, 7, 4, old, 'creation_result_unknown', None, None)
        assert row.accepted_at is not None
    assert not inspect(engine).get_foreign_keys('teams_recreation_decisions')


def test_decision_uniqueness(attempt_engine):
    with attempt_engine.begin() as conn:
        create_legacy_decisions(conn)
    ensure_application_schema(attempt_engine)
    new, old = str(uuid4()), str(uuid4())
    statement = text('INSERT INTO teams_recreation_decisions (job_id, requested_by_admin_id, expected_revision, replacement_transaction_id, prior_transaction_id, reason, prior_event_id, prior_join_url) VALUES (:job, 7, :revision, :new, :old, :reason, :event, :url)')
    base = {'job': 1, 'revision': 4, 'new': new, 'old': old, 'reason': 'event_missing', 'event': 'event', 'url': 'url'}
    with attempt_engine.begin() as conn:
        conn.execute(statement, base)
    for changes in ({'new': str(uuid4())}, {'job': 2}):
        with pytest.raises(IntegrityError):
            with attempt_engine.begin() as conn:
                conn.execute(statement, {**base, **changes})
    with attempt_engine.connect() as conn:
        assert conn.execute(text('SELECT prior_event_id, prior_join_url FROM teams_recreation_decisions')).one() == ('event', 'url')
