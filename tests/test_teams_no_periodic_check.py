from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from teams_identity_fixtures import IDENTITY
from test_teams_postgres import sessions
from test_teams_manual_reconciliation import _applied_job
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending


def test_synced_event_is_not_polled_and_due_retry_still_runs(sessions):
    _applied_job(sessions)
    start = datetime.now(timezone.utc) + timedelta(days=11)
    booking = SimpleNamespace(id=9903, starts_at=start, ends_at=start + timedelta(hours=1),
                              guest_name='Retry', guest_email='retry@example.org', user=None)
    with sessions.begin() as db:
        applied = db.scalar(select(TeamsSyncJob))
        applied.reconcile_at = datetime.now(timezone.utc) - timedelta(days=1)
        applied_id, revision = applied.id, applied.revision
        enqueue_booking(db, booking, 'upsert')
        retry = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_key == 'booking:9903'))
        retry.last_error = 'graph_error_429'
        retry.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    calls = []
    def fake(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(201, json={'id': 'retry-event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/test'}})
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
        assert not process_pending(sessions, lambda: IDENTITY, client)
    assert calls == ['POST']
    with sessions() as db:
        applied = db.get(TeamsSyncJob, applied_id)
        assert applied.revision == applied.applied_revision == revision
        assert applied.last_error is None
        retry = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_key == 'booking:9903'))
        assert retry.revision == retry.applied_revision
