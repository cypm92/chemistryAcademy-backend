from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, microsoft_oauth_routes
from app.database import Base, get_db
from app.main import app
from app.security import create_token
from app.teams_models import TeamsSyncJob


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        users = [models.User(name=x, email=x+'@example.com', password_hash='x', role='admin' if x == 'admin' else 'student') for x in ('admin', 'student', 'other')]
        db.add_all(users); db.flush()
        ids = [u.id for u in users]
        start = datetime.now(timezone.utc) + timedelta(days=10)
        booking = models.Booking(user_id=ids[1], starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); bid = booking.id
        db.add(TeamsSyncJob(booking_key='booking:'+str(bid), booking_id=bid, desired='upsert', transaction_id='transaction', revision=2, applied_revision=1, attempts=2, last_error='secret error', event_id='event', join_url='https://example.test/join'))
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield sessions, client, ids, bid
    app.dependency_overrides.clear(); engine.dispose()


def auth(uid):
    return {'Authorization': 'Bearer '+create_token(uid)}


def test_admin_state_and_student_privacy(context):
    sessions, client, (admin, student, other), bid = context
    row = client.get('/admin/classes', headers=auth(admin)).json()[0]
    assert row['teams_status'] == 'error'
    assert row['teams_join_url'] is None
    assert 'secret error' not in str(row)
    row = client.get('/classes', headers=auth(student)).json()[0]
    assert row['teams_status'] is None and row['teams_join_url'] is None
    assert client.get('/classes', headers=auth(other)).json() == []
    assert client.get('/admin/classes', headers=auth(student)).status_code == 403


def test_retry_existing_job_and_authorization(context):
    sessions, client, (admin, student, other), bid = context
    path = f'/admin/classes/{bid}/teams/retry'
    assert client.post(path, headers=auth(other)).status_code == 403
    response = client.post(path, headers=auth(admin))
    assert response.status_code == 200, response.text
    assert response.json()['teams_status'] == 'pending'
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert (job.transaction_id, job.event_id, job.attempts, job.last_error) == ('transaction', 'event', 0, None)


@pytest.mark.parametrize('applied,desired,booking_status,url,attempts,error,expected', [
    (1, 'upsert', 'confirmed', 'https://example.com/join', 0, None, 'pending'),
    (2, 'upsert', 'confirmed', 'https://example.com/join', 0, None, 'ready'),
    (2, 'upsert', 'confirmed', None, 0, None, 'pending'),
    (2, 'delete', 'cancelled', 'https://example.com/join', 0, None, None),
    (1, 'delete', 'cancelled', None, 0, None, 'pending'),
    (1, 'delete', 'cancelled', None, 1, 'private error', 'error'),
    (2, 'upsert', 'cancelled', 'https://example.com/join', 0, None, 'pending'),
    (1, 'upsert', 'confirmed', None, 0, 'private error', 'error'),
])
def test_status_matrix(context, applied, desired, booking_status, url, attempts, error, expected):
    sessions, client, (admin, student, other), bid = context
    with sessions.begin() as db:
        db.get(models.Booking, bid).status = booking_status
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.applied_revision, job.desired, job.join_url, job.attempts, job.last_error = applied, desired, url, attempts, error
    response = client.get('/admin/classes', headers=auth(admin))
    assert response.status_code == 200
    row = response.json()[0]
    assert row['teams_status'] == expected
    assert row['teams_join_url'] == (url if expected == 'ready' else None)
    assert 'private error' not in response.text


@pytest.mark.parametrize('condition,code', [('missing', 404), ('cancelled', 400), ('ready', 400), ('delete', 400)])
def test_retry_rejects_invalid_states(context, condition, code):
    sessions, client, (admin, student, other), bid = context
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        if condition == 'missing':
            db.delete(job)
            booking.starts_at = datetime.now(timezone.utc) - timedelta(days=7)
        elif condition == 'cancelled':
            booking.status = 'cancelled'
        elif condition == 'ready':
            job.applied_revision = job.revision
        else:
            job.desired = 'delete'
    assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == code
    with sessions() as db:
        assert (db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)) is None) == (condition == 'missing')


def test_legacy_and_mutation_response(context, monkeypatch):
    sessions, client, (admin, student, other), bid = context
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        db.delete(job)
    row = client.get('/admin/classes', headers=auth(admin)).json()[0]
    assert row['teams_status'] == 'not_configured'
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: object())
    row = client.get('/admin/classes', headers=auth(admin)).json()[0]
    assert row['teams_status'] is None
    response = client.patch(f'/admin/classes/{bid}/historical', headers=auth(admin), json={'is_historical': True})
    assert response.status_code == 200, response.text
    assert response.json()['teams_status'] is None


def test_failed_delete_retry_and_action_privacy(context):
    sessions, client, (admin, student, other), bid = context
    with sessions.begin() as db:
        db.get(models.Booking, bid).status = 'cancelled'
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.desired = 'delete'
        job.available_at = datetime.now(timezone.utc) + timedelta(days=1)
    row = client.get('/admin/classes', headers=auth(admin)).json()[0]
    assert (row['teams_status'], row['teams_action']) == ('error', 'delete')
    assert client.get('/classes', headers=auth(student)).json()[0]['teams_action'] is None
    path = f'/admin/classes/{bid}/teams/retry'
    assert client.post(path, headers=auth(student)).status_code == 403
    response = client.post(path, headers=auth(admin))
    assert response.status_code == 200, response.text
    assert (response.json()['teams_status'], response.json()['teams_action']) == ('pending', 'delete')
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert (job.desired, job.attempts, job.last_error, job.event_id) == ('delete', 0, None, 'event')
        assert job.available_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)


def test_upsert_action_visible_only_to_admin(context):
    sessions, client, (admin, student, other), bid = context
    assert client.get('/admin/classes', headers=auth(admin)).json()[0]['teams_action'] == 'upsert'
    assert client.get('/classes', headers=auth(student)).json()[0]['teams_action'] is None


def test_retry_cancelled_without_job_is_404(context):
    sessions, client, (admin, student, other), bid = context
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        booking.status = 'cancelled'
        booking.is_historical = True
        db.delete(db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)))
    assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 404
    with sessions() as db:
        assert db.scalar(select(TeamsSyncJob)) is None
