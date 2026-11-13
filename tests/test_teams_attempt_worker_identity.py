from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from ephemeral_postgres import postgres_test_db

from app.database import Base
from app.microsoft_refresh import MicrosoftAccessTokenIdentity
from app.microsoft_auth_models import MicrosoftOAuthGeneration
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending


def test_creation_attempt_committed_before_post(postgres_test_db):
    engine = postgres_test_db.engine
    assert engine.dialect.name == 'postgresql'
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    booking = SimpleNamespace(id=912, starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 2, tzinfo=timezone.utc), guest_name='A', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking, 'upsert')
    identity = MicrosoftAccessTokenIdentity('secret', 'tenant', 'account', 4)
    def respond(request):
        with sessions() as db:
            attempt = db.scalar(select(TeamsCreationAttempt))
            assert attempt is not None and attempt.state == 'in_flight'
            assert (attempt.tenant_id, attempt.account_object_id, attempt.oauth_generation) == ('tenant', 'account', 4)
        return httpx.Response(201, json={'id': 'remote', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/x'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert process_pending(sessions, lambda: identity, client)
    with sessions() as db:
        attempt = db.scalar(select(TeamsCreationAttempt))
        assert (attempt.state, attempt.event_id) == ('identified', 'remote')


def test_post_rejection_is_recorded_as_rejected(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    booking = SimpleNamespace(id=915, starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 2, tzinfo=timezone.utc), guest_name='A', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking, 'upsert')
    calls = []
    def respond(request):
        calls.append(request.method)
        return httpx.Response(403)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert process_pending(sessions, lambda: MicrosoftAccessTokenIdentity('secret', 'tenant', 'account', 4), client)
    with sessions() as db:
        assert calls == ['POST']
        assert db.scalar(select(TeamsCreationAttempt)).state == 'rejected'


def test_switched_identity_blocks_creation(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    booking = SimpleNamespace(id=913, starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 2, tzinfo=timezone.utc), guest_name='A', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=5, tenant_id='tenant', account_object_id='other'))
        enqueue_booking(db, booking, 'upsert')
    identity = MicrosoftAccessTokenIdentity('secret', 'tenant', 'account', 4)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))) as client:
        assert process_pending(sessions, lambda: identity, client)
    assert not calls
    with sessions() as db:
        assert db.scalar(select(TeamsCreationAttempt)) is None


def test_late_callback_preserves_new_selection(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    booking = SimpleNamespace(id=914, starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 2, tzinfo=timezone.utc), guest_name='A', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking, 'upsert')
    identity = MicrosoftAccessTokenIdentity('secret', 'tenant', 'account', 4)
    def respond(request):
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            job.transaction_id = 'new-selection'
            job.event_id = 'manual'
            job.join_url = 'https://manual'
        return httpx.Response(201, json={'id': 'late', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/x'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert process_pending(sessions, lambda: identity, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        attempt = db.scalar(select(TeamsCreationAttempt))
        assert (job.event_id, job.join_url, job.transaction_id) == ('manual', 'https://manual', 'new-selection')
        assert (attempt.state, attempt.event_id) == ('identified', 'late')
