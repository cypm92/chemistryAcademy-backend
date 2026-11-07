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
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob
from app.teams_sync import enqueue_booking
from app.teams_review_routes import PatchReviewBody, patch_review

TENANT = '11111111-1111-4111-8111-111111111111'
ACCOUNT = '33333333-3333-4333-8333-333333333333'


def test_concurrent_reviews_serialize(sessions):
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        gen = db.get(MicrosoftOAuthGeneration, 1)
        gen.tenant_id, gen.account_object_id = TENANT, ACCOUNT
        db.add(MicrosoftCredential(id=1, account_object_id=ACCOUNT, refresh_token_ciphertext='fake'))
        booking = models.Booking(guest_name='Ana', guest_email='a@example.test', status='confirmed',
                                 starts_at=now, ends_at=now + timedelta(hours=1))
        db.add(booking)
        db.flush()
        bid = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.event_id = 'event-1'
        revision = job.revision
        db.add(TeamsRemoteOperation(id=str(uuid4()), job_id=job.id, operation='patch', state='unknown',
            event_id=job.event_id, transaction_revision=revision, tenant_id=TENANT,
            account_object_id=ACCOUNT, oauth_generation=3))
    barrier = Barrier(2)
    def submit(_):
        barrier.wait(timeout=10)
        try:
            return patch_review(bid, PatchReviewBody(expected_revision=revision, acknowledge_checked=True,
                acknowledge_late_risk=True), SimpleNamespace(id=1), sessions).status_code
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == [200, 409]
    with sessions() as db:
        assert db.scalar(select(TeamsRemoteOperation)).reviewed_at is not None
        assert db.scalar(select(TeamsRemoteOperation)).state == 'unknown'
