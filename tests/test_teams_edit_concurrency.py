from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
import os
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending


@pytest.fixture
def pg_sessions():
    url = os.getenv('TEAMS_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    assert url.startswith(('postgresql://', 'postgresql+psycopg2://'))
    admin = create_engine(url)
    schema = 'teams_edit_' + uuid4().hex
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        seed_account(db)
    yield factory
    engine.dispose()
    with admin.begin() as db:
        db.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


def test_stale_patch_races_worker_completion(pg_sessions):
    sessions = pg_sessions
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        student = models.User(name='Student', email='student@example.com', password_hash='x', role='student')
        db.add_all([admin, student]); db.flush()
        booking = models.Booking(user_id=student.id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO', admin_comment='old')
        db.add(booking); db.flush(); admin_id, booking_id = admin.id, booking.id
        enqueue_booking(db, booking, 'upsert')
    entered, release = Event(), Event()
    errors = []
    def respond(request):
        entered.set()
        assert release.wait(10)
        return httpx.Response(201, json={'id': 'event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/l/meetup-join/x'}})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    responses, patch_started = [], Event()
    def patch_class():
        try:
            with TestClient(app) as client:
                patch_started.set()
                responses.append(client.patch(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'starts_at': start.isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': 's' * 1000}))
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker)
    patch_thread = Thread(target=patch_class)
    thread.start()
    try:
        assert entered.wait(10)
        patch_thread.start()
        assert patch_started.wait(10)
        release.set()
        thread.join(10); patch_thread.join(10)
        assert not thread.is_alive() and not patch_thread.is_alive() and not errors
        response = responses[0]
        assert response.status_code == 200, response.text
        with sessions() as db:
            booking = db.get(models.Booking, booking_id)
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
            assert job.join_url == 'https://teams.microsoft.com/l/meetup-join/x'
            assert booking.admin_comment == 's' * 1000 + '\nEnlace de Teams: https://teams.microsoft.com/l/meetup-join/x'
    finally:
        release.set()
        thread.join(10)
        app.dependency_overrides.clear()


def test_identity_change_while_worker_in_flight(pg_sessions):
    sessions = pg_sessions
    start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        student = models.User(name='Student', email='student@example.com', password_hash='x', role='student')
        db.add_all([admin, student]); db.flush()
        booking = models.Booking(user_id=student.id, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
        db.add(booking); db.flush(); admin_id, student_id, booking_id = admin.id, student.id, booking.id
        enqueue_booking(db, booking, 'upsert')
    entered, release = Event(), Event()
    errors = []
    def respond(request):
        entered.set()
        assert release.wait(10)
        return httpx.Response(201, json={'id': 'event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/l/meetup-join/x'}})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        with TestClient(app) as client:
            response = client.patch(f'/admin/users/{student_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'}, json={'name': 'New Name', 'email': 'new@example.com'})
        assert response.status_code == 200, response.text
        release.set(); thread.join(10)
        assert not thread.is_alive() and not errors
        with sessions() as db:
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
            assert job.revision == 2 and job.applied_revision < job.revision
            assert job.subject.startswith('New Name_') and 'new@example.com' not in job.subject
    finally:
        release.set(); thread.join(10)
        app.dependency_overrides.clear()
