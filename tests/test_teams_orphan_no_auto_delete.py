from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import models, teams_sync, microsoft_graph as graph
from app.database import Base
from app.teams_models import TeamsSyncJob


def test_legacy_orphan_is_not_deleted_automatically(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE teams_orphan_events (id INTEGER PRIMARY KEY, job_id INTEGER, event_id TEXT, state TEXT, completed_at TIMESTAMP)"))
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        job = TeamsSyncJob(booking_key='legacy', revision=1, desired='upsert',
                           transaction_id='transaction', applied_revision=1)
        db.add(job)
        db.flush()
        db.execute(text("INSERT INTO teams_orphan_events VALUES (1, :job, 'legacy-event', 'deleting', NULL)"), {'job': job.id})
    deleted = []
    monkeypatch.setattr(graph, 'delete_event', lambda client, token, event: deleted.append(event))
    with httpx.Client() as client:
        teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.execute(text('SELECT event_id, state, completed_at FROM teams_orphan_events')).one() == ('legacy-event', 'deleting', None)
    assert deleted == []


def test_late_creation_preserves_manual_selection_and_records_review(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sessions.begin() as db:
        seed_account(db)
        booking = models.Booking(starts_at=start, ends_at=start + timedelta(hours=1),
                                 status="confirmed", guest_name="Ana", guest_email="a@example.org")
        db.add(booking)
        db.flush()
        teams_sync.enqueue_booking(db, booking, "upsert")

    def create(*args):
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            job.event_id = "manual-selection"
            job.join_url = "manual-url"
        return "late-event", "late-url"

    monkeypatch.setattr(graph, "create_event", create)
    with httpx.Client() as client:
        assert teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        from app.teams_models import TeamsCreationAttempt
        attempt = db.scalar(select(TeamsCreationAttempt))
        assert (job.event_id, job.join_url) == ("manual-selection", "manual-url")
        assert (attempt.event_id, attempt.join_url, attempt.needs_review) == ("late-event", "late-url", True)
