from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import ensure_application_schema
from app import teams_models
from ephemeral_postgres import postgres_test_db


@pytest.fixture
def db(postgres_test_db):
    return postgres_test_db.engine


def test_legacy_migration_preserves_multiple_operations(db):
    with db.begin() as conn:
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, transaction_id VARCHAR(36) NOT NULL)'))
        conn.execute(text("INSERT INTO teams_sync_jobs VALUES (7, 2, 'legacy')"))
    ensure_application_schema(db)
    ensure_application_schema(db)
    model = teams_models.TeamsRemoteOperation
    ids = [str(uuid4()), str(uuid4())]
    with Session(db) as session:
        session.add_all([model(id=ids[0], job_id=7, operation='patch', state='in_flight', event_id='event', transaction_revision=1),
                         model(id=ids[1], job_id=7, operation='patch', state='unknown', event_id='event', transaction_revision=2)])
        session.commit()
    ensure_application_schema(db)
    with Session(db) as session:
        rows = session.query(model).order_by(model.transaction_revision).all()
        assert [(r.id, r.state) for r in rows] == list(zip(ids, ['in_flight', 'unknown']))
        assert all(r.created_at is not None and r.completed_at is None for r in rows)
        rows[0].state = 'completed'
        from datetime import datetime, timezone
        rows[0].completed_at = datetime.now(timezone.utc)
        session.commit()
    with Session(db) as session:
        assert session.get(model, ids[0]).state == 'completed'
        assert session.get(model, ids[0]).completed_at is not None
    inspector = inspect(db)
    assert not inspector.get_foreign_keys('teams_remote_operations')
    assert any(i['column_names'] == ['job_id'] for i in inspector.get_indexes('teams_remote_operations'))
    assert {c['name'] for c in inspector.get_columns('teams_remote_operations')} == {'id', 'job_id', 'operation', 'state', 'event_id', 'transaction_revision', 'tenant_id', 'account_object_id', 'oauth_generation', 'created_at', 'completed_at', 'reviewed_at', 'reviewed_by_admin_id', 'reviewed_revision', 'reviewed_desired'}


@pytest.mark.parametrize('operation,state', [('delete', 'in_flight'), ('patch', 'invalid')])
def test_invalid_operation_and_state_rejected(db, operation, state):
    ensure_application_schema(db)
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text('INSERT INTO teams_remote_operations (id, job_id, operation, state, event_id, transaction_revision) VALUES (:id, 1, :operation, :state, :event, 1)'), {'id': str(uuid4()), 'operation': operation, 'state': state, 'event': 'event'})
