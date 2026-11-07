from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
import httpx
import pytest
from sqlalchemy import select
from app import models
from app.teams_models import TeamsSyncJob
from app.teams_sync import process_pending
from test_teams_api import context, auth


@pytest.mark.parametrize('applied', [True, False])
def test_missing_event_fails_closed(context, applied):
    sessions, api, (admin, student, _), bid = context
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        booking.admin_comment = 'Nota manual\nEnlace de Teams: https://example.test/join'
        job = db.scalar(select(TeamsSyncJob))
        seed_account(db)
        seed_identified_event(db, job)
        job.last_error = 'manual_reconcile' if applied else None
        job.starts_at, job.ends_at, job.subject = booking.starts_at, booking.ends_at, 'Test'
        job.attempts = 0
        job.applied_revision = job.revision if applied else job.revision - 1
    calls = []
    def graph(request):
        calls.append(request.method)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(graph)) as graph_client:
        assert process_pending(sessions, lambda: IDENTITY, graph_client)
        for _ in range(3):
            process_pending(sessions, lambda: IDENTITY, graph_client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.last_error == 'event_missing'
        assert job.event_id == 'event'
        assert db.get(models.Booking, bid).admin_comment == 'Nota manual'
    assert 'POST' not in calls
    row = api.get('/admin/classes', headers=auth(admin)).json()[0]
    assert row['teams_status'] == 'needs_review'
    assert row['teams_review_reason'] == 'event_missing'
    assert api.get('/classes', headers=auth(student)).json()[0]['teams_review_reason'] is None
    assert api.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 409


@pytest.mark.parametrize('status', [401, 403, 429])
def test_reconciliation_provider_errors_visible_without_creation(context, status):
    sessions, api, (admin, _, _), bid = context
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        job = db.scalar(select(TeamsSyncJob))
        seed_account(db)
        seed_identified_event(db, job)
        job.starts_at, job.ends_at, job.subject = booking.starts_at, booking.ends_at, 'Test'
        job.applied_revision, job.last_error, job.attempts = job.revision, 'manual_reconcile', 0
    calls = []
    def graph(request):
        calls.append(request.method)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(graph)) as graph_client:
        assert process_pending(sessions, lambda: IDENTITY, graph_client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.last_error == f'graph_error_{status}'
        assert job.event_id == 'event'
    assert calls == ['GET']
    assert api.get('/admin/classes', headers=auth(admin)).json()[0]['teams_status'] == 'error'
    assert api.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 200
