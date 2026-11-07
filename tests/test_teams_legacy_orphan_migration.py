"""Legacy orphan evidence must survive the transition to creation attempts."""
import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, ensure_application_schema
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob


def test_matched_legacy_orphan_requires_manual_review_without_mutation():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS teams_orphan_events'))
        conn.execute(text('CREATE TABLE teams_orphan_events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, event_id TEXT NOT NULL, completed_at TIMESTAMP)'))
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        job = TeamsSyncJob(booking_key='legacy-test', desired='upsert', transaction_id='legacy-job', revision=1)
        db.add(job)
        db.flush()
        job_id = job.id
        db.add(TeamsCreationAttempt(job_id=job_id, transaction_id='existing', revision=1, state='identified', event_id='existing-event'))
        db.execute(text("INSERT INTO teams_orphan_events (id, job_id, event_id) VALUES (1, :job, 'existing-event')"), {'job': job_id})
    with pytest.raises(RuntimeError, match='manual review'):
        ensure_application_schema(engine)
    with sessions() as db:
        attempts = list(db.scalars(select(TeamsCreationAttempt).where(TeamsCreationAttempt.job_id == job_id)))
        assert {(a.event_id, a.needs_review) for a in attempts} == {('existing-event', False)}
        assert db.execute(text('SELECT count(*) FROM teams_orphan_events')).scalar_one() == 1



def test_unmatched_legacy_orphan_fails_closed_without_inventing_attempt():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS teams_orphan_events'))
        conn.execute(text('CREATE TABLE teams_orphan_events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, event_id TEXT NOT NULL, completed_at TIMESTAMP)'))
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        job = TeamsSyncJob(booking_key='unmatched', desired='upsert', transaction_id='real-job', revision=1)
        db.add(job); db.flush()
        db.execute(text("INSERT INTO teams_orphan_events (id, job_id, event_id) VALUES (1, :job, 'unmatched-event')"), {'job': job.id})
    with pytest.raises(RuntimeError, match='manual review'):
        ensure_application_schema(engine)
    with sessions() as db:
        assert db.scalar(select(TeamsCreationAttempt)) is None
        assert db.execute(text('SELECT event_id FROM teams_orphan_events')).scalar_one() == 'unmatched-event'
