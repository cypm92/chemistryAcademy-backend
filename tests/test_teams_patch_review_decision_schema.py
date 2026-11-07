import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import ensure_application_schema
from app import teams_models


@pytest.fixture(params=['sqlite', 'postgresql'])
def db(request, tmp_path):
    admin = None
    if request.param == 'sqlite':
        engine = create_engine(f'sqlite:///{tmp_path / "review.sqlite"}', connect_args={'timeout': 10})
    else:
        url = os.getenv('MICROSOFT_SCHEMA_TEST_POSTGRES_URL')
        if not url:
            pytest.skip('MICROSOFT_SCHEMA_TEST_POSTGRES_URL must be a disposable database')
        if not url.startswith(('postgresql://', 'postgresql+psycopg2://')):
            pytest.fail('PostgreSQL required')
        admin = create_engine(url)
        schema = 'patch_review_' + uuid4().hex
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        yield engine
    finally:
        engine.dispose()
        if admin:
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()



def test_fresh_schema_has_operation_review_fields_and_no_decision_table(db):
    ensure_application_schema(db)
    ensure_application_schema(db)
    inspector = inspect(db)
    assert not inspector.has_table('teams_patch_review_decisions')
    assert {'reviewed_at', 'reviewed_by_admin_id', 'reviewed_revision', 'reviewed_desired'} <= {
        col['name'] for col in inspector.get_columns('teams_remote_operations')}


def test_legacy_upgrade_preserves_decisions_but_requires_review_again(db):
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE teams_remote_operations (id VARCHAR(36) PRIMARY KEY, job_id INTEGER NOT NULL, operation VARCHAR(16) NOT NULL, state VARCHAR(16) NOT NULL, event_id TEXT NOT NULL, transaction_revision INTEGER NOT NULL, tenant_id VARCHAR(36), account_object_id VARCHAR(36), oauth_generation INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, completed_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO teams_remote_operations (id, job_id, operation, state, event_id, transaction_revision) VALUES ('op', 42, 'patch', 'unknown', 'event', 1)"))
        conn.execute(text("CREATE TABLE teams_patch_review_decisions (id INTEGER PRIMARY KEY, operation_id VARCHAR(36) NOT NULL)"))
        conn.execute(text("INSERT INTO teams_patch_review_decisions VALUES (1, 'op')"))
    ensure_application_schema(db)
    ensure_application_schema(db)
    with db.connect() as conn:
        assert conn.execute(text('SELECT id, operation_id FROM teams_patch_review_decisions')).all() == [(1, 'op')]
        assert conn.execute(text('SELECT state, reviewed_at, reviewed_by_admin_id, reviewed_revision, reviewed_desired FROM teams_remote_operations')).one() == ('unknown', None, None, None, None)
