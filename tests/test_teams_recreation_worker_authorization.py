import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from ephemeral_postgres import postgres_test_db

from app import models, teams_sync
from app.database import Base
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob
from teams_identity_fixtures import IDENTITY, seed_account

OLD = '11111111-1111-4111-8111-111111111111'
NEW = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


def setup_job(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    booking = SimpleNamespace(id=912, starts_at=start, ends_at=start + timedelta(hours=1),
                              guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        seed_account(db)
        teams_sync.enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.transaction_id = NEW
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=OTHER, revision=1, state='identified', event_id='old-event', needs_review=True))
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=OLD, revision=1,
                                    state='unknown'))
    return sessions


def authorize(job, transaction=NEW):
    job.recreation_transaction_id = transaction
    job.recreation_at = datetime.now(timezone.utc)
    job.recreation_admin_id = 1
    job.recreation_revision = 1
    job.recreation_reason = 'creation_result_unknown'
    job.recreation_tenant_id = IDENTITY.tenant_id
    job.recreation_account_id = IDENTITY.account_object_id


def decision(job_id, transaction=NEW):
    return TeamsCreationAttempt(job_id=job_id, revision=2, state='rejected',
                                   authorized_at=datetime.now(timezone.utc),
                                   transaction_id=transaction,
                                   tenant_id=IDENTITY.tenant_id,
                                   account_object_id=IDENTITY.account_object_id)


@pytest.mark.parametrize('authorization,desired,expected', [
    ('exact', 'upsert', 1), ('none', 'upsert', 0), ('other_job', 'upsert', 0),
    ('other_transaction', 'upsert', 0), ('exact', 'delete', 0),
])
def test_only_exact_authorization_allows_recreation_with_orphan(authorization, desired, expected, postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.desired = desired
        if authorization == 'exact':
            authorize(job)
        elif authorization == 'other_transaction':
            authorize(job, OTHER)
        elif authorization == 'other_job':
            db.add(decision(job.id + 1))
    calls = []
    def graph(request):
        calls.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(201, json={'id': 'new-event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}})
    with httpx.Client(transport=httpx.MockTransport(graph)) as client:
        teams_sync.process_pending(sessions, lambda: IDENTITY, client)
        teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    assert [method for method, _ in calls] == ['POST'] * expected
    if expected:
        assert calls[0][1]['transactionId'] == NEW
    with sessions() as db:
        orphan = db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == OTHER))
        assert (orphan.event_id, orphan.needs_review) == ('old-event', True)
        old = db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == OLD))
        assert old.state == 'unknown'
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == ('new-event' if expected else None)


def test_incomplete_authorization_cannot_send(postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        authorize(job)
        job.recreation_admin_id = None
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201))) as client:
        teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    assert calls == []


def test_late_old_reply_records_review_without_touching_new_lease(postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.transaction_id = OLD
        for attempt in db.scalars(select(TeamsCreationAttempt)):
            db.delete(attempt)
    def graph(request):
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            db.add(decision(job.id))
            job.transaction_id = NEW
            job.event_id = 'selected-event'
            job.join_url = 'selected-url'
            job.lease_token = 'new-lease'
            job.lease_expires_at = datetime(2030, 1, 1)
            job.creation_in_flight = True
        return httpx.Response(201, json={'id': 'late-old-event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/old'}})
    with httpx.Client(transport=httpx.MockTransport(graph)) as client:
        assert teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert (job.transaction_id, job.event_id, job.join_url, job.lease_token, job.creation_in_flight) == (NEW, 'selected-event', 'selected-url', 'new-lease', True)
        assert job.lease_expires_at == datetime(2030, 1, 1, tzinfo=timezone.utc)
        attempt = db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == OLD))
        assert (attempt.transaction_id, attempt.state, attempt.event_id) == (OLD, 'identified', 'late-old-event')
        assert attempt.needs_review
