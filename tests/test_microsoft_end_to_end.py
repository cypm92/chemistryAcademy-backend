"""Synthetic OAuth-to-Teams flows using the real application and worker."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, notification_worker as worker
from app.database import Base, get_db
from app.main import app
from app.microsoft_oauth_routes import get_http_transport, get_session_factory
from app.security import create_token
from app.teams_models import TeamsSyncJob, utcnow


@pytest.fixture
def engine(tmp_path):
    db = create_engine('sqlite:///' + str(tmp_path / 'e2e.db'), connect_args={'check_same_thread': False})
    yield db
    db.dispose()


@pytest.fixture
def scenario(engine, monkeypatch):
    for key, value in {
        'MICROSOFT_TENANT_ID': '11111111-1111-4111-8111-111111111111',
        'MICROSOFT_CLIENT_ID': '22222222-2222-4222-8222-222222222222',
        'MICROSOFT_CLIENT_SECRET': 'fake-secret',
        'MICROSOFT_REDIRECT_URI': 'https://example.test/api/integrations/microsoft/callback',
        'MICROSOFT_FERNET_KEY': Fernet.generate_key().decode(),
        'MICROSOFT_SYNC_ENABLED': 'true',
    }.items():
        monkeypatch.setenv(key, value)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin', is_active=True)
        student = models.User(name='Student', email='student@example.com', password_hash='x', role='student', is_active=True)
        db.add_all([admin, student]); db.flush()
        admin_id, student_id = admin.id, student.id
    calls = []
    throttle = [False]
    events = {}
    join_url = 'https://teams.microsoft.com/l/meetup-join/synthetic'
    def handler(request):
        calls.append(request)
        if request.url.host == 'login.microsoftonline.com':
            return httpx.Response(200, json={'access_token': 'fake-access', 'refresh_token': 'fake-refresh'})
        assert request.url.host == 'graph.microsoft.com'
        if request.url.path == '/v1.0/me':
            return httpx.Response(200, json={'id': '33333333-3333-4333-8333-333333333333', 'displayName': 'Test'})
        if request.method == 'POST' and request.url.path == '/v1.0/me/events':
            if throttle[0]:
                throttle[0] = False
                return httpx.Response(429, headers={'Retry-After': '60'})
            assert 'event-1' not in events
            events['event-1'] = {**json.loads(request.content), 'id': 'event-1', 'onlineMeeting': {'joinUrl': join_url}}
            return httpx.Response(201, json=events['event-1'])
        if request.url.path == '/v1.0/me/events/event-1':
            if 'event-1' not in events:
                return httpx.Response(404)
            if request.method == 'GET':
                return httpx.Response(200, json=events['event-1'])
            if request.method == 'PATCH':
                payload = json.loads(request.content)
                events['event-1'].update({key: payload[key] for key in ('subject', 'start', 'end') if key in payload})
                return httpx.Response(200, json=events['event-1'])
            if request.method == 'DELETE':
                del events['event-1']
                return httpx.Response(204)
        raise AssertionError(f'unexpected request {request.method} {request.url}')
    transport = httpx.MockTransport(handler)
    def database():
        with sessions() as db:
            yield db
    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_http_transport] = lambda: transport
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    monkeypatch.setattr(worker, 'engine', engine)
    # These functions use the application's default global engine; this schema is already fresh.
    for name in ('ensure_application_schema', 'ensure_notification_lease_column', 'ensure_booking_management_column'):
        monkeypatch.setattr(worker, name, lambda *args: None)
    # Mail is outside this test: never attempt SMTP.
    monkeypatch.setattr(worker, 'deliver_pending', lambda factory: 0)
    real_client = httpx.Client
    try:
        with TestClient(app, base_url='https://example.test', follow_redirects=False) as client:
            auth = lambda uid: {'Authorization': 'Bearer ' + create_token(uid)}
            started = client.post('/admin/integrations/microsoft/start', headers=auth(admin_id))
            assert started.status_code == 200, started.text
            state = parse_qs(urlparse(started.json()['authorization_url']).query)['state'][0]
            nonce = client.cookies.get('microsoft_oauth_nonce')
            result = client.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'}, headers={'Cookie': 'microsoft_oauth_nonce=' + nonce})
            assert result.headers['location'] == '/admin?microsoft=connected'
            assert client.get('/admin/integrations/microsoft', headers=auth(admin_id)).json()['connected']
            monkeypatch.setattr(worker.httpx, 'Client', lambda **kwargs: real_client(transport=transport, **kwargs))
            yield sessions, client, auth(admin_id), auth(student_id), calls, throttle, join_url, events
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


def start_time():
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    return start + timedelta(days=(-start.weekday()) % 7)


def test_guest_booking_reconcile_retry_edit_cancel(scenario, monkeypatch):
    sessions, client, admin, student, calls, throttle, join, events = scenario
    start = start_time()
    response = client.post('/public/trial-bookings', json={'name': 'Invitada', 'email': 'guest@example.com', 'subject': 'Química ESO', 'starts_at': start.isoformat()})
    assert response.status_code == 201, response.text
    bid = response.json()['id']
    response = client.patch(f'/admin/bookings/{bid}', headers=admin, json={'status': 'confirmed'})
    assert response.status_code == 200, response.text
    calls.clear()
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'false')
    worker.run_once()
    assert not calls
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'true')
    throttle[0] = True
    worker.run_once()
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.last_error == 'graph_error_429' and job.attempts == 1
        assert job.event_id is None
        job.available_at = utcnow() - timedelta(seconds=1)
    worker.run_once()
    posts = [r for r in calls if r.method == 'POST' and r.url.host == 'graph.microsoft.com']
    assert len(posts) == 2
    first, second = [json.loads(r.content) for r in posts]
    assert first == second and first['transactionId']
    assert first['subject'] == f"Invitada_{start.astimezone(ZoneInfo('Europe/Madrid')):%d-%m-%Y %H:%M}"
    assert 'attendees' not in first
    assert first['start'] == {'dateTime': start.replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'}
    assert first['end'] == {'dateTime': (start + timedelta(hours=1)).replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'}
    assert any(r.url.host == 'login.microsoftonline.com' and parse_qs(r.content.decode()).get('grant_type') == ['refresh_token'] for r in calls)
    before_reconcile = json.loads(json.dumps(events))
    worker.run_once()
    assert not any(r.method == 'GET' and r.url.path == '/v1.0/me/events/event-1' for r in calls)
    assert events == before_reconcile
    assert len([r for r in calls if r.method == 'POST' and r.url.host == 'graph.microsoft.com']) == 2
    with sessions() as db:
        assert join in db.get(models.Booking, bid).admin_comment
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.event_id == 'event-1' and job.revision == job.applied_revision
    row = next(r for r in client.get('/admin/classes', headers=admin).json() if r['id'] == bid)
    assert row['teams_status'] == 'ready' and row['teams_join_url'] == join
    changed = start + timedelta(hours=2)
    response = client.patch(f'/admin/classes/{bid}', headers=admin, json={'starts_at': changed.isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': row['admin_comment']})
    assert response.status_code == 200, response.text
    worker.run_once()
    patches = [r for r in calls if r.method == 'PATCH']
    assert len(patches) == 1 and json.loads(patches[0].content)['start']['dateTime'] == changed.replace(tzinfo=None).isoformat()
    patch = json.loads(patches[0].content)
    assert all(events['event-1'][key] == patch[key] for key in ('subject', 'start', 'end'))
    assert events['event-1']['onlineMeeting']['joinUrl'] == join
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.reconcile_at = utcnow() - timedelta(seconds=1)
    before_reconcile = json.loads(json.dumps(events))
    get_count = sum(r.method == 'GET' and r.url.path == '/v1.0/me/events/event-1' for r in calls)
    worker.run_once()
    assert sum(r.method == 'GET' and r.url.path == '/v1.0/me/events/event-1' for r in calls) == get_count
    assert events == before_reconcile
    assert len([r for r in calls if r.method == 'PATCH']) == 1
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.last_error is None and job.revision == job.applied_revision
    response = client.post(f'/admin/classes/{bid}/cancel', headers=admin)
    assert response.status_code == 200, response.text
    worker.run_once()
    assert len([r for r in calls if r.method == 'DELETE']) == 1
    assert not events
    with sessions() as db:
        assert join not in (db.get(models.Booking, bid).admin_comment or '')
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.join_url is None and job.event_id is None


def test_registered_student_sees_reconciled_link(scenario):
    sessions, client, admin, student, calls, throttle, join, events = scenario
    response = client.post('/bookings', headers=student, json={'subject': 'Química ESO', 'starts_at': start_time().isoformat(), 'duration_slots': 2})
    assert response.status_code == 201, response.text
    bid = response.json()['id']
    response = client.patch(f'/admin/bookings/{bid}', headers=admin, json={'status': 'confirmed'})
    assert response.status_code == 200, response.text
    worker.run_once()
    row = next(r for r in client.get('/classes', headers=student).json() if r['id'] == bid)
    assert join in row['admin_comment']


def test_full_length_comment_can_be_edited_after_teams_creation(scenario):
    sessions, client, admin, student, calls, throttle, join, events = scenario
    with sessions() as db:
        student_id = db.scalar(select(models.User.id).where(models.User.role == 'student'))
    start = start_time()
    text = 'a' * 1000
    response = client.post('/admin/classes', headers=admin, json={
        'user_id': student_id, 'starts_at': start.isoformat(), 'duration_slots': 2,
        'subject': 'Química ESO', 'admin_comment': text})
    assert response.status_code == 201, response.text
    bid = response.json()['id']
    worker.run_once()
    row = next(r for r in client.get('/admin/classes', headers=admin).json() if r['id'] == bid)
    assert row['admin_comment'] == text + '\nEnlace de Teams: ' + join
    response = client.patch(f'/admin/classes/{bid}', headers=admin, json={
        'starts_at': (start + timedelta(hours=2)).isoformat(), 'duration_slots': 2,
        'subject': 'Química ESO', 'admin_comment': row['admin_comment']})
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'] == row['admin_comment']
    edited = ' ' + 'b' * 998 + ' '
    payload = {'starts_at': (start + timedelta(hours=2)).isoformat(), 'duration_slots': 2,
               'subject': 'Química ESO', 'admin_comment': edited + '\nEnlace de Teams: ' + join}
    response = client.patch(f'/admin/classes/{bid}', headers=admin, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'] == payload['admin_comment']
    for bad in (edited + 'x', edited + '\nEnlace de Teams: https://evil.example/x',
                edited + '\nEnlace de Teams: ' + join + '\nEnlace de Teams: ' + join):
        response = client.patch(f'/admin/classes/{bid}', headers=admin, json={**payload, 'admin_comment': bad})
        assert response.status_code == 422, response.text
    with sessions() as db:
        assert db.get(models.Booking, bid).admin_comment == payload['admin_comment']
    student_row = next(r for r in client.get('/classes', headers=student).json() if r['id'] == bid)
    assert student_row['admin_comment'] == edited
    assert join not in str(student_row)
    worker.run_once()
    student_row = next(r for r in client.get('/classes', headers=student).json() if r['id'] == bid)
    assert student_row['admin_comment'] == payload['admin_comment']
    response = client.post(f'/admin/classes/{bid}/cancel', headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'] == edited
