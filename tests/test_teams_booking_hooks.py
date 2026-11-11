from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, microsoft_oauth_routes
from app.database import Base, get_db
from app.main import app
from app.microsoft_auth_models import MicrosoftOAuthGeneration
from app.security import create_token
from app.teams_models import TeamsSyncJob


def test_confirmed_guest_request_enqueues_after_binding(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    tenant = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: SimpleNamespace(tenant=tenant))
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
        start += timedelta(days=(-start.weekday()) % 7)
        booking = models.Booking(guest_name='Guest', guest_email='guest@example.com', starts_at=start,
                                 ends_at=start + timedelta(hours=1), status='requested', note='Chemistry')
        db.add_all([admin, booking, MicrosoftOAuthGeneration(id=1, generation=1, account_object_id='33333333-3333-4333-8333-333333333333', tenant_id=tenant)])
        db.flush()
        admin_id, booking_id = admin.id, booking.id
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    try:
        client = TestClient(app)
        response = client.patch(f'/admin/bookings/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'status': 'confirmed'})
        client.close()
        assert response.status_code == 200, response.text
        with sessions() as db:
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
            assert job is not None
            assert job.desired == 'upsert'
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def booking_context(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        student = models.User(name='Student', email='student@example.com', password_hash='x', role='student')
        db.add_all([admin, student]); db.flush()
        ids = admin.id, student.id
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield sessions, client, ids
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize('route', ['guest', 'student', 'direct'])
@pytest.mark.parametrize('binding', ['enabled', 'disabled', 'disconnected', 'wrong_tenant'])
def test_confirmation_and_creation_respect_binding(booking_context, monkeypatch, route, binding):
    sessions, client, (admin_id, student_id) = booking_context
    tenant = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None if binding == 'disabled' else SimpleNamespace(tenant=tenant))
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        if binding in ('enabled', 'disabled', 'wrong_tenant'):
            db.add(MicrosoftOAuthGeneration(id=1, generation=1, account_object_id='account', tenant_id=tenant if binding != 'wrong_tenant' else 'other'))
        if route != 'direct':
            booking = models.Booking(user_id=student_id if route == 'student' else None,
                guest_name='Guest' if route == 'guest' else None, guest_email='guest@example.com' if route == 'guest' else None,
                starts_at=start, ends_at=start + timedelta(hours=1), status='requested', note='Química ESO')
            db.add(booking); db.flush(); booking_id = booking.id
    headers = {'Authorization': f'Bearer {create_token(admin_id)}'}
    if route == 'direct':
        response = client.post('/admin/classes', headers=headers, json={'user_id': student_id, 'starts_at': start.isoformat(), 'subject': 'Química ESO', 'duration_slots': 2})
        assert response.status_code == 201, response.text
        booking_id = response.json()['id']
    else:
        response = client.patch(f'/admin/bookings/{booking_id}', headers=headers, json={'status': 'confirmed'})
        assert response.status_code == 200, response.text
    with sessions() as db:
        assert db.get(models.Booking, booking_id).status == 'confirmed'
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert (job is not None) == (binding == 'enabled')
        if job:
            assert job.desired == 'upsert'
            assert ('guest@example.com' if route == 'guest' else 'student@example.com') not in job.subject


@pytest.mark.parametrize('route', ['guest', 'student', 'direct'])
def test_enqueue_failure_rolls_back_booking(booking_context, monkeypatch, route):
    from app import main
    sessions, client, (admin_id, student_id) = booking_context
    tenant = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: SimpleNamespace(tenant=tenant))
    def fail_enqueue(*args):
        raise RuntimeError('enqueue failed')
    monkeypatch.setattr(main, 'enqueue_teams_booking', fail_enqueue)
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=1, account_object_id='account', tenant_id=tenant))
        if route != 'direct':
            booking = models.Booking(user_id=student_id if route == 'student' else None,
                guest_name='Guest' if route == 'guest' else None, guest_email='guest@example.com' if route == 'guest' else None,
                starts_at=start, ends_at=start + timedelta(hours=1), status='requested', note='Química ESO')
            db.add(booking); db.flush(); booking_id = booking.id
    headers = {'Authorization': f'Bearer {create_token(admin_id)}'}
    if route == 'direct':
        response = client.post('/admin/classes', headers=headers, json={'user_id': student_id, 'starts_at': start.isoformat(), 'subject': 'Química ESO', 'duration_slots': 2})
    else:
        response = client.patch(f'/admin/bookings/{booking_id}', headers=headers, json={'status': 'confirmed'})
    assert response.status_code == 500
    with sessions() as db:
        if route == 'direct':
            assert db.scalar(select(models.Booking)) is None
        else:
            assert db.get(models.Booking, booking_id).status == 'requested'
        assert db.scalar(select(TeamsSyncJob)) is None


def test_reschedule_existing_job_when_disconnected(booking_context, monkeypatch):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO', admin_comment='manual URL')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': (start + timedelta(hours=1)).isoformat(), 'subject': 'Química ESO', 'duration_slots': 2, 'admin_comment': 'manual URL'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert job.revision == 2
        assert job.desired == 'upsert'
        assert db.get(models.Booking, booking_id).admin_comment == 'manual URL'


@pytest.mark.parametrize('route', ['student', 'admin_guest', 'admin_past'])
def test_cancel_existing_job_without_configuration(booking_context, monkeypatch, route):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    start = (datetime.now(timezone.utc) + timedelta(days=-14 if route == 'admin_past' else 14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(user_id=None if route == 'admin_guest' else student_id, guest_name='Guest' if route == 'admin_guest' else None, guest_email='g@example.com' if route == 'admin_guest' else None, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    path = f'/bookings/{booking_id}/cancel' if route == 'student' else f'/admin/classes/{booking_id}/cancel'
    response = client.post(path, headers={'Authorization': f'Bearer {create_token(student_id if route == "student" else admin_id)}'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert (job.revision, job.desired) == (2, 'delete')
        assert db.get(models.Booking, booking_id).status == 'cancelled'


@pytest.mark.parametrize('binding', ['enabled', 'disabled', 'disconnected', 'wrong_tenant'])
@pytest.mark.parametrize('change', ['subject', 'duration', 'time', 'comment', 'unchanged', 'requested'])
def test_edit_without_existing_job_never_creates_one(booking_context, monkeypatch, binding, change):
    sessions, client, (admin_id, student_id) = booking_context
    tenant = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None if binding == 'disabled' else SimpleNamespace(tenant=tenant))
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        if binding != 'disconnected':
            db.add(MicrosoftOAuthGeneration(id=1, generation=1, account_object_id='account', tenant_id='other' if binding == 'wrong_tenant' else tenant))
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='requested' if change == 'requested' else 'confirmed', note='Química ESO', admin_comment='manual URL')
        db.add(booking); db.flush(); booking_id = booking.id
    payload = {'starts_at': (start + timedelta(hours=1) if change == 'time' else start).isoformat(), 'duration_slots': 3 if change == 'duration' else 2, 'subject': 'Física ESO' if change in ('subject', 'requested') else 'Química ESO', 'admin_comment': 'manual URL updated' if change == 'comment' else 'manual URL'}
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json=payload)
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert job is None
        assert db.get(models.Booking, booking_id).admin_comment == payload['admin_comment']


@pytest.mark.parametrize('route', ['student_cancel', 'admin_cancel', 'edit'])
def test_hook_failure_rolls_back_mutation(booking_context, monkeypatch, route):
    from app import main
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(user_id=None if route == 'admin_cancel' else student_id, guest_name='Guest' if route == 'admin_cancel' else None, guest_email='g@example.com' if route == 'admin_cancel' else None, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    def fail_enqueue(*args):
        raise RuntimeError('enqueue failed')
    monkeypatch.setattr(main, 'enqueue_teams_booking', fail_enqueue)
    headers = {'Authorization': f'Bearer {create_token(student_id if route == "student_cancel" else admin_id)}'}
    if route == 'edit':
        response = client.patch(f'/admin/classes/{booking_id}', headers=headers, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Física ESO', 'admin_comment': ''})
    else:
        path = f'/bookings/{booking_id}/cancel' if route == 'student_cancel' else f'/admin/classes/{booking_id}/cancel'
        response = client.post(path, headers=headers)
    assert response.status_code == 500
    with sessions() as db:
        booking = db.get(models.Booking, booking_id)
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert (booking.status, booking.note, job.revision, job.desired) == ('confirmed', 'Química ESO', 1, 'upsert')


@pytest.mark.parametrize('route', ['student', 'admin'])
def test_cancel_without_job_never_creates_one(booking_context, monkeypatch, route):
    sessions, client, (admin_id, student_id) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: SimpleNamespace(tenant='tenant'))
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=1, account_object_id='account', tenant_id='tenant'))
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
    path = f'/bookings/{booking_id}/cancel' if route == 'student' else f'/admin/classes/{booking_id}/cancel'
    response = client.post(path, headers={'Authorization': f'Bearer {create_token(student_id if route == "student" else admin_id)}'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        assert db.scalar(select(TeamsSyncJob)) is None


@pytest.mark.parametrize('change', ['subject', 'comment', 'unchanged'])
def test_existing_guest_job_subject_diff_and_noop(booking_context, monkeypatch, change):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, _) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(guest_name='Guest', guest_email='g@example.com', starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO', admin_comment='https://manual.example')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    with sessions() as db:
        old_subject = db.scalar(select(TeamsSyncJob.subject))
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Física ESO' if change == 'subject' else 'Química ESO', 'admin_comment': 'https://manual.example' + (' updated' if change == 'comment' else '')})
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert job.revision == (2 if change == 'subject' else 1)
        assert job.subject == old_subject
        assert db.get(models.Booking, booking_id).admin_comment.startswith('https://manual.example')


def test_stale_comment_preserves_managed_link(booking_context):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO', admin_comment='old comment')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        job.join_url = 'https://managed.example/join'
        booking.admin_comment += '\nEnlace de Teams: https://managed.example/join'
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': 'new manual comment'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == 'new manual comment\nEnlace de Teams: https://managed.example/join'


def test_email_only_change_does_not_reenqueue_teams_job(booking_context):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    response = client.patch(f'/admin/users/{student_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'email': 'new@example.com'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert job.revision == 1
        assert db.get(models.User, student_id).email == 'new@example.com'


def test_user_identity_change_updates_existing_job_only(booking_context, monkeypatch):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        managed = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        legacy = models.Booking(user_id=student_id, starts_at=start + timedelta(days=1), ends_at=start + timedelta(days=1, hours=1), status='confirmed', note='Química ESO')
        db.add_all([managed, legacy]); db.flush(); managed_id, legacy_id = managed.id, legacy.id
        enqueue_booking(db, managed, 'upsert')
    response = client.patch(f'/admin/users/{student_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'name': 'New Name', 'email': 'NEW@example.com'})
    assert response.status_code == 200, response.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == managed_id))
        assert job.revision == 2
        assert job.subject.startswith('New Name_') and 'new@example.com' not in job.subject
        assert db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == legacy_id)) is None


@pytest.mark.parametrize('desired,status,expected', [('upsert', 'confirmed', 1), ('delete', 'confirmed', 0), ('upsert', 'requested', 0)])
def test_comment_merge_respects_job_and_booking_state(booking_context, desired, status, expected):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    url = 'https://managed.example/join'
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status=status, note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, desired)
        db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id)).join_url = url
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': 'manual https://other.example'})
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'].count(url) == expected
    assert 'manual https://other.example' in response.json()['admin_comment']


def test_comment_with_current_link_is_not_duplicated(booking_context):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    url = 'https://managed.example/join'
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
        db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id)).join_url = url
    comment = 'manual' + chr(10) + 'Enlace de Teams: ' + url
    response = client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': comment})
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'] == comment


@pytest.mark.parametrize('route', ['student', 'admin'])
@pytest.mark.parametrize('managed_present', [True, False])
def test_cancel_removes_only_managed_link_immediately(booking_context, monkeypatch, route, managed_present):
    from app.teams_sync import enqueue_booking
    sessions, client, (admin_id, student_id) = booking_context
    monkeypatch.setattr(microsoft_oauth_routes, 'get_config', lambda: None)
    start = datetime.now(timezone.utc) + timedelta(days=14)
    url = 'https://managed.example/join'
    manual = 'Notas manuales\nEnlace de Teams: https://other.example/join'
    with sessions.begin() as db:
        booking = models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO', admin_comment=manual + ('\nEnlace de Teams: ' + url if managed_present else ''))
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
        db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id)).join_url = url
    path = f'/bookings/{booking_id}/cancel' if route == 'student' else f'/admin/classes/{booking_id}/cancel'
    response = client.post(path, headers={'Authorization': f'Bearer {create_token(student_id if route == "student" else admin_id)}'})
    assert response.status_code == 200, response.text
    assert response.json()['admin_comment'] == manual
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == manual
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        assert job.desired == 'delete' and job.join_url == url
