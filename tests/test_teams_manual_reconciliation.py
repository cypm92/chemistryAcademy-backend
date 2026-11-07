"""Independent reconciliation of remote calendar drift (fake Graph only)."""
from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
from sqlalchemy import select

from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, _reconcile_one
from test_teams_postgres import sessions


def _applied_job(sessions):
    start = datetime.now(timezone.utc) + timedelta(days=10)
    booking = SimpleNamespace(id=9902, starts_at=start, ends_at=start + timedelta(hours=1),
                              guest_name='Old', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = 'event'
        job.join_url = 'https://teams.microsoft.com/test'
        job.applied_revision = job.revision
        seed_identified_event(db, job)
    return booking


def _state(subject, booking):
    return {'subject': subject,
            'start': {'dateTime': booking.starts_at.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': booking.ends_at.isoformat(), 'timeZone': 'UTC'},
            'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/test'}}


def test_expired_reconcile_get_is_fenced_by_new_worker(sessions):
    from threading import Event, Thread
    booking = _applied_job(sessions)
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).last_error = 'manual_reconcile'
    entered, release = Event(), Event()
    errors, calls = [], []

    def slow(request):
        calls.append('slow')
        entered.set()
        assert release.wait(15)
        return httpx.Response(200, json=_state('stale', booking))

    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(slow)) as client:
                assert _reconcile_one(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        with httpx.Client(transport=httpx.MockTransport(slow)) as client:
            assert not _reconcile_one(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            subject = job.subject
            job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        def current(request):
            calls.append('current')
            return httpx.Response(200, json=_state(subject, booking))
        with httpx.Client(transport=httpx.MockTransport(current)) as client:
            assert _reconcile_one(sessions, lambda: IDENTITY, client)
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    assert calls == ['slow', 'current']
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.revision == job.applied_revision
        assert job.lease_token is None and job.reconcile_at is not None
