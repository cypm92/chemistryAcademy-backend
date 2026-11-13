from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from ephemeral_postgres import postgres_test_db

from app import models, teams_sync
from app.database import Base
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob


@pytest.fixture
def sessions(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    factory = postgres_test_db.session_factory
    with factory.begin() as db:
        seed_account(db)
    return factory


def booking():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return models.Booking(starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', guest_name='Ana', guest_email='a@example.org', admin_comment='manual')


def test_detach_isolates_reused_id_and_preserves_orphan(sessions, monkeypatch):
    from app import microsoft_graph as graph
    with sessions.begin() as db:
        old = booking(); db.add(old); db.flush(); old_id = old.id
        teams_sync.enqueue_booking(db, old, 'upsert')
        job = db.scalar(select(TeamsSyncJob)); job.event_id = 'old-event'; job.join_url = 'old-url'
        seed_identified_event(db, job)
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=1, state='identified', event_id='orphan', needs_review=True))
        teams_sync.enqueue_booking(db, old, 'delete')
        teams_sync.detach_deleted_booking(db, old)
        old_job_id = job.id
        db.delete(old)
    with sessions.begin() as db:
        new = booking()
        new.id = old_id  # Explicitly model a recycled sequence value.
        db.add(new); db.flush()
        assert new.id == old_id
        teams_sync.enqueue_booking(db, new, 'upsert')
        jobs = db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.id)).all()
        assert len(jobs) == 2
        assert jobs[0].id == old_job_id and jobs[0].booking_id is None
        assert jobs[0].booking_key.startswith('deleted:') and jobs[0].event_id == 'old-event'
        assert jobs[1].booking_id == old_id and jobs[1].event_id is None
        assert db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.event_id == 'orphan')).job_id == old_job_id
    deleted = []
    monkeypatch.setattr(graph, 'delete_event', lambda client, token, event: deleted.append(event))
    monkeypatch.setattr(graph, 'create_event', lambda *args: ('new-event', 'https://teams.microsoft.com/new'))
    with httpx.Client() as client:
        for _ in range(2):
            assert teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    assert set(deleted) == {'old-event'}
    with sessions() as db:
        assert db.get(models.Booking, old_id).admin_comment == 'manual\nEnlace de Teams: https://teams.microsoft.com/new'
        assert db.get(TeamsSyncJob, old_job_id).event_id is None
        assert db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.event_id == 'orphan')).needs_review


def test_detach_legacy_and_rollback(sessions):
    with sessions.begin() as db:
        old = booking(); db.add(old); db.flush(); old_id = old.id
        assert teams_sync.detach_deleted_booking(db, old) is None
        assert db.scalar(select(TeamsSyncJob)) is None
        teams_sync.enqueue_booking(db, old, 'upsert')
    with pytest.raises(RuntimeError):
        with sessions.begin() as db:
            old = db.get(models.Booking, old_id)
            teams_sync.enqueue_booking(db, old, 'delete')
            teams_sync.detach_deleted_booking(db, old)
            db.delete(old)
            db.flush()
            raise RuntimeError('rollback')
    with sessions() as db:
        assert db.get(models.Booking, old_id) is not None
        job = db.scalar(select(TeamsSyncJob))
        assert (job.booking_key, job.booking_id, job.desired, job.revision) == (f'booking:{old_id}', old_id, 'upsert', 1)


def test_detach_rejects_job_not_marked_delete(sessions):
    with sessions.begin() as db:
        old = booking(); db.add(old); db.flush()
        teams_sync.enqueue_booking(db, old, 'upsert')
        with pytest.raises(ValueError):
            teams_sync.detach_deleted_booking(db, old)
