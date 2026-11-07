"""Fail-closed creation tests; PostgreSQL requires a disposable database URL."""
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base, ensure_application_schema
from app.teams_models import TeamsSyncJob, utcnow
from app.teams_sync import enqueue_booking, process_pending
from app.microsoft_refresh import MicrosoftAccessTokenIdentity
from app.microsoft_auth_models import MicrosoftOAuthGeneration

IDENTITY = MicrosoftAccessTokenIdentity('token', 'tenant', 'account', 4)


def booking():
    return SimpleNamespace(id=321, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana',
        guest_email='a@example.org', user=None)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_explicit_creation_rejection_retries_with_same_transaction(status):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking(), 'upsert')
    requests = []
    def response(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={'Retry-After': '120'})
        return httpx.Response(201, json={'id': 'event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            assert not job.creation_in_flight
            assert job.last_error == f'graph_error_{status}'
            assert job.attempts == 1
            if status == 429:
                assert (job.available_at - utcnow().replace(tzinfo=None)).total_seconds() > 110
            job.available_at = utcnow() - timedelta(seconds=1)
        assert process_pending(sessions, lambda: IDENTITY, client)
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == 'event' and job.applied_revision == job.revision


@pytest.mark.parametrize('status', [201, 500, 503])
def test_ambiguous_creation_stays_unknown_after_revisions(status):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking(), 'upsert')
    calls = []
    def response(request):
        calls.append(request.method)
        return httpx.Response(status, json={})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            enqueue_booking(db, booking(), 'delete')
        assert not process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.creation_in_flight and job.last_error == 'creation_result_unknown'
        assert job.applied_revision < job.revision
    assert calls == ['POST']


def test_sqlite_legacy_migration_is_idempotent():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, booking_key VARCHAR(80) NOT NULL UNIQUE, booking_id INTEGER, revision INTEGER NOT NULL, desired VARCHAR(10) NOT NULL, transaction_id VARCHAR(36) NOT NULL, subject VARCHAR(500), starts_at TIMESTAMP, ends_at TIMESTAMP, event_id TEXT, join_url TEXT, applied_revision INTEGER NOT NULL, attempts INTEGER NOT NULL, available_at TIMESTAMP NOT NULL, lease_token VARCHAR(36), lease_expires_at TIMESTAMP, last_error TEXT)'))
        db.execute(text("INSERT INTO teams_sync_jobs (id, booking_key, revision, desired, transaction_id, applied_revision, attempts, available_at) VALUES (1, 'booking:1', 1, 'upsert', '99e6c369-1065-4356-9982-ced46867142b', 0, 0, CURRENT_TIMESTAMP)"))
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT creation_in_flight, last_error FROM teams_sync_jobs')).one() == (1, 'creation_result_unknown')
        assert db.execute(text('SELECT job_id, transaction_id, revision, state FROM teams_creation_attempts')).all() == [(1, '99e6c369-1065-4356-9982-ced46867142b', 1, 'unknown')]


def crash_worker(url, schema, entered, release):
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    def accepted_then_crash(request):
        entered.set()
        release.wait(15)
        os._exit(17)
    with httpx.Client(transport=httpx.MockTransport(accepted_then_crash)) as client:
        process_pending(sessionmaker(engine), lambda: IDENTITY, client)


def test_postgres_legacy_migration_is_idempotent():
    url = os.environ.get('TEAMS_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    admin = create_engine(url)
    schema = 'teams_migration_' + uuid4().hex
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.begin() as db:
            db.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, booking_key VARCHAR(80) NOT NULL UNIQUE, booking_id INTEGER, revision INTEGER NOT NULL, desired VARCHAR(10) NOT NULL, transaction_id VARCHAR(36) NOT NULL, subject VARCHAR(500), starts_at TIMESTAMP, ends_at TIMESTAMP, event_id TEXT, join_url TEXT, applied_revision INTEGER NOT NULL, attempts INTEGER NOT NULL, available_at TIMESTAMP NOT NULL, lease_token VARCHAR(36), lease_expires_at TIMESTAMP, last_error TEXT)'))
            db.execute(text("INSERT INTO teams_sync_jobs (id, booking_key, revision, desired, transaction_id, applied_revision, attempts, available_at) VALUES (1, 'booking:1', 1, 'upsert', '99e6c369-1065-4356-9982-ced46867142b', 0, 0, CURRENT_TIMESTAMP)"))
        ensure_application_schema(engine)
        ensure_application_schema(engine)
        with engine.connect() as db:
            assert db.execute(text('SELECT creation_in_flight, last_error FROM teams_sync_jobs')).one() == (True, 'creation_result_unknown')
            assert db.execute(text('SELECT job_id, transaction_id, revision, state FROM teams_creation_attempts')).all() == [(1, '99e6c369-1065-4356-9982-ced46867142b', 1, 'unknown')]
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def test_postgres_process_crash_and_cancel_do_not_replay():
    url = os.environ.get('TEAMS_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    admin = create_engine(url)
    schema = 'teams_unknown_' + uuid4().hex
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        Base.metadata.create_all(engine)
        ensure_application_schema(engine)
        ensure_application_schema(engine)
        sessions = sessionmaker(engine)
        with sessions.begin() as db:
            db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
            enqueue_booking(db, booking(), 'upsert')
        ctx = multiprocessing.get_context('spawn')
        entered = ctx.Event()
        release = ctx.Event()
        worker = ctx.Process(target=crash_worker, args=(url, schema, entered, release))
        worker.start()
        assert entered.wait(15)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            assert job.creation_in_flight
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
            enqueue_booking(db, booking(), 'delete')
        release.set()
        worker.join(15)
        assert worker.exitcode == 17
        def forbidden(request):
            raise AssertionError('unexpected Graph request')
        with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
            assert not process_pending(sessions, lambda: IDENTITY, client)
        with sessions() as db:
            job = db.scalar(select(TeamsSyncJob))
            assert job.creation_in_flight and job.last_error == 'creation_result_unknown'
            assert job.applied_revision < job.revision
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def test_malformed_legacy_schema_fails_closed_without_fabricating_identity():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, revision INTEGER)'))
    with pytest.raises(RuntimeError, match='transaction_id'):
        ensure_application_schema(engine)
    with engine.connect() as db:
        assert not db.execute(text("SELECT 1 FROM sqlite_master WHERE name='teams_creation_attempts'")).first()
