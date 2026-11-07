from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from test_teams_postgres import sessions
from app import models
from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob
from app.teams_sync import enqueue_booking
from app.teams_review_routes import RecreateBody, recreate


def test_approved_account_switch_never_posts(sessions):
    import httpx
    from app.microsoft_refresh import MicrosoftAccessTokenIdentity
    from app.teams_sync import process_pending
    tenant = '11111111-1111-4111-8111-111111111111'
    account_a = '33333333-3333-4333-8333-333333333333'
    account_b = '44444444-4444-4444-8444-444444444444'
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        gen = db.get(MicrosoftOAuthGeneration, 1)
        gen.tenant_id, gen.account_object_id = tenant, account_a
        db.add(MicrosoftCredential(id=1, account_object_id=account_a, refresh_token_ciphertext='fake'))
        booking = models.Booking(guest_name='Ana', guest_email='a@example.test', status='confirmed', starts_at=now, ends_at=now + timedelta(hours=1))
        db.add(booking); db.flush(); bid = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.last_error = 'creation_result_unknown'; job.creation_in_flight = True
        revision = job.revision
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id, revision=revision,
            tenant_id=tenant, account_object_id=account_a, oauth_generation=4, state='unknown'))
    assert recreate(bid, RecreateBody(expected_revision=revision, confirm_absent=True), SimpleNamespace(id=1), sessions).status_code == 200
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert (job.recreation_tenant_id, job.recreation_account_id) == (tenant, account_a)
        assert db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == job.transaction_id)) is None
        gen = db.get(MicrosoftOAuthGeneration, 1)
        gen.account_object_id = account_b
        gen.generation += 1
        db.get(MicrosoftCredential, 1).account_object_id = account_b
        generation = gen.generation
    calls = []
    identity = MicrosoftAccessTokenIdentity('fake-token', tenant, account_b, generation)
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201, json={'id': 'event'}))) as client:
        process_pending(sessions, lambda: identity, client)
        process_pending(sessions, lambda: identity, client)
    assert calls == []
    with sessions() as db:
        assert db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)).last_error == 'recreation_account_mismatch'
    with sessions.begin() as db:
        gen = db.get(MicrosoftOAuthGeneration, 1)
        gen.account_object_id = account_a
        gen.generation += 1
        generation = gen.generation
        db.get(MicrosoftCredential, 1).account_object_id = account_a
    from app.main import retry_class_teams
    with sessions() as db:
        retry_class_teams(bid, None, db)
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201, json={'id': 'event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}}))) as client:
        process_pending(sessions, lambda: MicrosoftAccessTokenIdentity('original', tenant, account_a, generation), client)
    assert len(calls) == 1 and calls[0].headers['authorization'] == 'Bearer original'


def test_simultaneous_confirmations_only_one_decision(sessions):
    tenant = '11111111-1111-4111-8111-111111111111'
    account = '33333333-3333-4333-8333-333333333333'
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        gen = db.get(MicrosoftOAuthGeneration, 1)
        gen.tenant_id, gen.account_object_id = tenant, account
        db.add(MicrosoftCredential(id=1, account_object_id=account, refresh_token_ciphertext='fake'))
        booking = models.Booking(guest_name='Ana', guest_email='a@example.test', status='confirmed', starts_at=now, ends_at=now + timedelta(hours=1))
        db.add(booking); db.flush(); bid = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.last_error = 'creation_result_unknown'; job.creation_in_flight = True
        revision = job.revision
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id, revision=revision,
            tenant_id=tenant, account_object_id=account, oauth_generation=4, state='unknown'))
    barrier = Barrier(2)
    def submit():
        barrier.wait(timeout=10)
        try:
            return recreate(bid, RecreateBody(expected_revision=revision, confirm_absent=True), SimpleNamespace(id=1), sessions).status_code
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(results) == [200, 409]
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.recreation_at is not None and job.recreation_transaction_id == job.transaction_id
        assert db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == job.transaction_id)) is None
        assert job.revision == revision + 1
