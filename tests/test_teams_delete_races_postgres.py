"""Physical deletion races against fake Graph on isolated PostgreSQL schemas."""
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, models
from app.database import get_db
from app.security import create_token
from app.teams_models import TeamsCreationAttempt, TeamsSyncJob, utcnow
from app.teams_sync import process_pending
from teams_identity_fixtures import IDENTITY, seed_identified_event
from test_teams_delete_hooks import add_booking
from test_teams_postgres import sessions


@pytest.fixture
def api(sessions):
    with sessions.begin() as db:
        a = models.User(name='Admin', email='race-admin@example.com', password_hash='x', role='admin')
        u = models.User(name='Student', email='race-student@example.com', password_hash='x', role='student')
        db.add_all([a, u]); db.flush()
        ids = a.id, u.id
    def database():
        with sessions() as db:
            yield db
    main.app.dependency_overrides[get_db] = database
    client = TestClient(main.app)
    try:
        yield client, ids, {'Authorization': f'Bearer {create_token(ids[0])}'}
    finally:
        client.close()
        main.app.dependency_overrides.pop(get_db, None)


def worker(sessions, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return process_pending(sessions, lambda: IDENTITY, client)


def make_event(sessions, user):
    with sessions.begin() as db:
        bid = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.event_id = 'remote-event'
        job.join_url = 'https://teams.microsoft.com/event'
        seed_identified_event(db, job)
        job.applied_revision = job.revision
    return bid


def test_failed_remote_delete_keeps_class_and_exposes_error(sessions, api):
    client, (_, user), headers = api
    bid = make_event(sessions, user)
    assert client.post(f'/admin/classes/{bid}/cancel', headers=headers).status_code == 200
    entered, release = Event(), Event()
    errors = []
    def graph(req):
        assert req.method == 'DELETE'
        entered.set(); assert release.wait(10)
        return httpx.Response(500, json={'error': {'code': 'ServerError'}})
    def run():
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run); thread.start()
    try:
        assert entered.wait(10)
        response = client.delete(f'/admin/classes/{bid}', headers=headers)
        assert response.status_code == 409, response.text
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        booking = db.get(models.Booking, bid)
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert booking is not None and job.event_id == 'remote-event' and job.last_error
        assert main.class_data(booking, job)['teams_status'] == 'error'
    assert client.delete(f'/admin/classes/{bid}', headers=headers).status_code == 409


def test_user_delete_is_atomic_with_multiple_bookings(sessions, api):
    client, (_, user), headers = api
    first, second = make_event(sessions, user), make_event(sessions, user)
    assert client.post(f'/admin/classes/{first}/cancel', headers=headers).status_code == 200
    assert worker(sessions, lambda req: httpx.Response(204))
    response = client.delete(f'/admin/users/{user}', headers=headers)
    assert response.status_code == 409, response.text
    with sessions() as db:
        assert db.get(models.User, user) is not None
        assert all(db.get(models.Booking, i) is not None for i in (first, second))
        assert {j.booking_id for j in db.scalars(select(TeamsSyncJob))} == {first, second}
    assert client.post(f'/admin/classes/{second}/cancel', headers=headers).status_code == 200
    assert worker(sessions, lambda req: httpx.Response(204))
    assert client.delete(f'/admin/users/{user}', headers=headers).status_code == 204
    with sessions() as db:
        assert db.get(models.User, user) is None
        assert all(db.get(models.Booking, i) is None for i in (first, second))
        assert all(j.booking_id is None for j in db.scalars(select(TeamsSyncJob)))


@pytest.mark.parametrize('method', ['POST', 'PATCH'])
def test_expired_inflight_request_blocks_physical_delete(sessions, api, method):
    client, (_, user), headers = api
    if method == 'PATCH':
        bid = make_event(sessions, user)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
            job.applied_revision = 0
    else:
        with sessions.begin() as db:
            bid = add_booking(db, user)
    entered, release = Event(), Event()
    errors, remote = [], {'remote-event'} if method == 'PATCH' else set()
    def graph(req):
        assert req.method == method
        entered.set(); assert release.wait(10)
        if method == 'POST':
            remote.add('remote-event')
            return httpx.Response(201, json={'id': 'remote-event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/event'}})
        return httpx.Response(200, json={})
    def run():
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run); thread.start()
    try:
        assert entered.wait(10)
        assert client.post(f'/admin/classes/{bid}/cancel', headers=headers).status_code == 200
        with sessions.begin() as db:
            db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)).lease_expires_at = utcnow() - timedelta(seconds=1)
        if method == 'PATCH':
            def delete(req):
                assert req.method == 'DELETE'
                remote.remove('remote-event')
                return httpx.Response(204)
            assert worker(sessions, delete)
        response = client.delete(f'/admin/classes/{bid}', headers=headers)
        assert response.status_code == 409, response.text
        with sessions() as db:
            assert db.get(models.Booking, bid) is not None
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    if method == 'POST':
        def delete(req):
            assert req.method == 'DELETE'
            remote.remove('remote-event')
            return httpx.Response(204)
        assert worker(sessions, delete)
    assert not remote
    assert client.delete(f'/admin/classes/{bid}', headers=headers).status_code == 204


def test_concurrent_cancel_then_delete_serializes(sessions, api, monkeypatch):
    client, (_, user), headers = api
    bid = make_event(sessions, user)
    entered, release, deleting = Event(), Event(), Event()
    results, errors = {}, []
    original = main.enqueue_existing_teams_delete
    def pause(db, booking):
        entered.set()
        assert release.wait(10)
        return original(db, booking)
    monkeypatch.setattr(main, 'enqueue_existing_teams_delete', pause)
    def request(name, call):
        try:
            if name == 'delete':
                deleting.set()
            results[name] = call()
        except BaseException as exc:
            errors.append(exc)
    cancel = Thread(target=request, args=('cancel', lambda: client.post(f'/admin/classes/{bid}/cancel', headers=headers)))
    delete = Thread(target=request, args=('delete', lambda: client.delete(f'/admin/classes/{bid}', headers=headers)))
    cancel.start()
    try:
        assert entered.wait(10)
        delete.start(); assert deleting.wait(10)
    finally:
        release.set(); cancel.join(15); delete.join(15)
    assert not cancel.is_alive() and not delete.is_alive() and not errors
    assert results['cancel'].status_code == 200
    assert results['delete'].status_code == 409
    with sessions() as db:
        assert db.get(models.Booking, bid).status == 'cancelled'
        assert db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)).desired == 'delete'


def test_ambiguous_creation_blocks_user_deletion(sessions, api):
    client, (_, user), headers = api
    with sessions.begin() as db:
        first = add_booking(db, user)
        second = add_booking(db, user)
        jobs = list(db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.id)))
        for job in jobs:
            job.desired = 'delete'; job.applied_revision = job.revision
        db.add(TeamsCreationAttempt(job_id=jobs[1].id, transaction_id=str(uuid4()), revision=jobs[1].revision, state='unknown'))
    assert client.delete(f'/admin/users/{user}', headers=headers).status_code == 409
    with sessions() as db:
        assert db.get(models.User, user) is not None
        assert all(db.get(models.Booking, i) is not None for i in (first, second))
        assert {j.booking_id for j in db.scalars(select(TeamsSyncJob))} == {first, second}
