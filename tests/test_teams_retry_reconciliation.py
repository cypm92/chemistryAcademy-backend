from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import models
from app.teams_models import TeamsSyncJob
from app.teams_sync import _reconcile_one
from test_teams_api import context, auth
from test_teams_postgres import sessions
from test_teams_manual_reconciliation import _applied_job


@pytest.mark.parametrize('header,seconds', [('900', 900), ('0', 300), ('9999999999', 86400), ('invalid', 300)])
def test_reconcile_respects_sanitized_retry_after(sessions, header, seconds):
    _applied_job(sessions)
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).last_error = 'manual_reconcile'
    start = datetime.now(timezone.utc)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={'Retry-After': header}))) as client:
        assert _reconcile_one(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert seconds <= (job.reconcile_at - start).total_seconds() < seconds + 10
        assert job.last_error == 'graph_error_429'


@pytest.mark.parametrize('ended_on_request', [True, False])
def test_ended_reconciliation_retry_executes_one_get(context, ended_on_request):
    sessions, client, (admin, _, _), bid = context
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        booking.ends_at = datetime.now(timezone.utc) + timedelta(seconds=-1 if ended_on_request else 60)
        job = db.scalar(select(TeamsSyncJob))
        job.ends_at = booking.ends_at
        seed_account(db)
        seed_identified_event(db, job)
        job.applied_revision = job.revision
        job.last_error = 'graph_error_429'
    response = client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin))
    assert response.status_code == 200, response.text
    assert response.json()['teams_status'] == 'pending'
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    calls = []
    def graph(request):
        calls.append(request.method)
        return httpx.Response(429, headers={'Retry-After': '900'})
    with httpx.Client(transport=httpx.MockTransport(graph)) as graph_client:
        assert _reconcile_one(sessions, lambda: IDENTITY, graph_client)
        assert not _reconcile_one(sessions, lambda: IDENTITY, graph_client)
    assert calls == ['GET']
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.last_error == 'graph_error_429'
        assert job.applied_revision == job.revision
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).reconcile_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with httpx.Client(transport=httpx.MockTransport(graph)) as graph_client:
        assert not _reconcile_one(sessions, lambda: IDENTITY, graph_client)
    assert calls == ['GET']


def test_manual_retry_keeps_existing_throttle_deadline(context):
    sessions, client, (admin, _, _), bid = context
    deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.applied_revision = job.revision
        job.last_error = 'graph_error_429'
        job.reconcile_at = deadline
    response = client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin))
    assert response.status_code == 200
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.reconcile_at.replace(tzinfo=timezone.utc) == deadline


def test_cancel_while_manual_get_is_in_flight(sessions):
    from threading import Event, Thread
    from app.teams_sync import enqueue_booking
    booking = _applied_job(sessions)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        job.last_error = 'manual_reconcile'
    entered, release = Event(), Event()
    errors, calls = [], []
    def graph(request):
        calls.append(request.method)
        entered.set()
        assert release.wait(10)
        return httpx.Response(404)
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(graph)) as client:
                assert _reconcile_one(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            enqueue_booking(db, booking, 'delete')
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors and calls == ['GET']
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.desired == 'delete'
        assert job.last_error != 'event_missing'
        assert job.applied_revision < job.revision
