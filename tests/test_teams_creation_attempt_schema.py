import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import ensure_application_schema
from app.teams_models import TeamsCreationAttempt


def test_fresh_attempt_schema_omits_unused_legacy_fields():
    engine = create_engine('sqlite://')
    ensure_application_schema(engine)
    columns = {c['name'] for c in inspect(engine).get_columns('teams_creation_attempts')}
    assert not columns.intersection({'authorized_by_admin_id', 'authorization_reason', 'expected_revision',
                                     'prior_transaction_id', 'prior_event_id', 'prior_join_url'})
    assert {'authorized_at', 'dispatched_at'} <= columns


def test_legacy_attempt_columns_and_values_remain_untouched():
    engine = create_engine('sqlite://')
    TeamsCreationAttempt.__table__.create(engine)
    names = ('authorized_by_admin_id', 'authorization_reason', 'expected_revision',
             'prior_transaction_id', 'prior_event_id', 'prior_join_url')
    ensure_application_schema(engine)
    assert not set(names) & {c['name'] for c in inspect(engine).get_columns('teams_creation_attempts')}
    with engine.begin() as conn:
        for name in names:
            conn.execute(text(f'ALTER TABLE teams_creation_attempts ADD COLUMN {name} TEXT'))
        conn.execute(text("INSERT INTO teams_creation_attempts (job_id, transaction_id, revision, state, "
                          + ', '.join(names) + ") VALUES (1, 'txn', 1, 'unknown', "
                          + ', '.join("'evidence'" for _ in names) + ')'))
    ensure_application_schema(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT ' + ', '.join(names) + ' FROM teams_creation_attempts')).one() == ('evidence',) * 6


@pytest.fixture(params=['sqlite', 'postgresql'])
def attempt_engine(request, tmp_path):
    admin = None
    if request.param == 'sqlite':
        engine = create_engine(f'sqlite:///{tmp_path / "attempts.sqlite"}')
    else:
        url = os.getenv('MICROSOFT_SCHEMA_TEST_POSTGRES_URL')
        if not url:
            pytest.skip('MICROSOFT_SCHEMA_TEST_POSTGRES_URL must be a disposable database')
        if not url.startswith(('postgresql://', 'postgresql+psycopg2://')):
            pytest.fail('PostgreSQL required')
        admin = create_engine(url)
        schema = 'attempt_test_' + uuid4().hex
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


def test_attempt_constraints_and_index(attempt_engine):
    TeamsCreationAttempt.__table__.create(attempt_engine)
    txn = str(uuid4())
    insert = text('INSERT INTO teams_creation_attempts (job_id, transaction_id, revision, state) VALUES (1, :txn, 1, :state)')
    with attempt_engine.begin() as conn:
        conn.execute(insert, {'txn': txn, 'state': 'in_flight'})
    for bad_txn, state in [(txn, 'unknown'), (str(uuid4()), 'invalid')]:
        with pytest.raises(IntegrityError):
            with attempt_engine.begin() as conn:
                conn.execute(insert, {'txn': bad_txn, 'state': state})
    with attempt_engine.connect() as conn:
        created, updated = conn.execute(text('SELECT created_at, updated_at FROM teams_creation_attempts')).one()
        assert created is not None and updated is not None
    assert any(i['column_names'] == ['job_id', 'state'] for i in inspect(attempt_engine).get_indexes('teams_creation_attempts'))


def test_legacy_attempt_is_durable_and_migration_idempotent(attempt_engine):
    engine = attempt_engine
    transaction_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, transaction_id VARCHAR(36) NOT NULL, revision INTEGER NOT NULL, creation_in_flight BOOLEAN NOT NULL)'))
        conn.execute(text('INSERT INTO teams_sync_jobs VALUES (1, :txn, 4, TRUE)'), {'txn': transaction_id})
        conn.execute(text('INSERT INTO teams_sync_jobs VALUES (2, :txn, 1, FALSE)'), {'txn': str(uuid4())})
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with engine.begin() as conn:
        rows = conn.execute(text('SELECT job_id, transaction_id, revision, tenant_id, account_object_id, oauth_generation, state FROM teams_creation_attempts')).all()
        assert rows == [(1, transaction_id, 4, None, None, None, 'unknown')]
        conn.execute(text("UPDATE teams_creation_attempts SET state = 'identified', event_id = 'event'"))
    ensure_application_schema(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state, event_id FROM teams_creation_attempts')).one() == ('identified', 'event')
    assert any(index['column_names'] == ['job_id', 'state'] for index in inspect(engine).get_indexes('teams_creation_attempts'))


def test_missing_transaction_id_requires_intervention_without_inventing_identity(attempt_engine):
    with attempt_engine.begin() as conn:
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, event_id TEXT, applied_revision INTEGER, revision INTEGER, last_error TEXT)'))
        conn.execute(text('INSERT INTO teams_sync_jobs VALUES (1, NULL, 0, 1, NULL)'))
    for _ in range(2):
        with pytest.raises(RuntimeError, match='transaction_id.*explicit intervention'):
            ensure_application_schema(attempt_engine)
    with attempt_engine.connect() as conn:
        assert conn.execute(text('SELECT id, event_id, applied_revision, revision, last_error FROM teams_sync_jobs')).one() == (1, None, 0, 1, None)
    assert 'transaction_id' not in {c['name'] for c in inspect(attempt_engine).get_columns('teams_sync_jobs')}


@pytest.mark.parametrize('transaction_id', [None, ''])
def test_in_flight_missing_identity_requires_intervention(attempt_engine, transaction_id):
    with attempt_engine.begin() as conn:
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, transaction_id VARCHAR(36), revision INTEGER NOT NULL, creation_in_flight BOOLEAN NOT NULL)'))
        conn.execute(text('INSERT INTO teams_sync_jobs VALUES (1, :txn, 1, TRUE)'), {'txn': transaction_id})
    with pytest.raises(RuntimeError, match='transaction_id.*explicit intervention'):
        ensure_application_schema(attempt_engine)
    with attempt_engine.connect() as conn:
        assert conn.execute(text('SELECT transaction_id FROM teams_sync_jobs')).scalar_one() == transaction_id
