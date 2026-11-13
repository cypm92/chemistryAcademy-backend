from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from ephemeral_postgres import postgres_test_db

from app import main, models
from app.database import Base, get_db
from app.security import create_token
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking


@pytest.fixture
def ctx(postgres_test_db):
    engine = postgres_test_db.engine
    assert engine.dialect.name == 'postgresql'
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        user = models.User(name='Student', email='student@example.com', password_hash='x', role='student')
        db.add_all([admin, user]); db.flush()
        ids = admin.id, user.id
    def database():
        with sessions() as db:
            yield db
    main.app.dependency_overrides[get_db] = database
    client = TestClient(main.app, raise_server_exceptions=False)
    try:
        yield sessions, client, ids
    finally:
        client.close()
        main.app.dependency_overrides.clear()
        engine.dispose()


def add_booking(db, user_id, job=True, days=14, status='confirmed', booking_id=None):
    start = datetime.now(timezone.utc) + timedelta(days=days)
    booking = models.Booking(id=booking_id, user_id=user_id, starts_at=start, ends_at=start + timedelta(hours=1), status=status, note='Chemistry')
    db.add(booking); db.flush()
    if job:
        enqueue_booking(db, booking, 'upsert')
    return booking.id


@pytest.mark.parametrize('route', ['class', 'user'])
def test_delete_rejects_pending_teams_without_mutating_job(ctx, route):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        booking_id = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = 'remote-event'
    path = f'/admin/classes/{booking_id}' if route == 'class' else f'/admin/users/{user}'
    response = client.delete(path, headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == 409
    with sessions() as db:
        assert db.get(models.Booking, booking_id) is not None
        job = db.scalar(select(TeamsSyncJob))
        assert (job.booking_id, job.desired, job.revision, job.event_id) == (booking_id, 'upsert', 1, 'remote-event')


@pytest.mark.parametrize('condition', ['clean', 'revision', 'event', 'join', 'lease', 'expired_lease', 'flight', 'unknown', 'orphan', 'rejected'])
def test_clean_delete_requires_all_durable_evidence_resolved(ctx, condition):
    from uuid import uuid4
    from app.teams_models import TeamsCreationAttempt
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        booking_id = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        job.desired = 'delete'; job.applied_revision = job.revision
        if condition == 'revision': job.applied_revision = 0
        if condition == 'event': job.event_id = 'event'
        if condition == 'join': job.join_url = 'url'
        if condition in ('lease', 'expired_lease'):
            job.lease_token = str(uuid4())
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30 if condition == 'lease' else -30)
        if condition == 'flight': job.creation_in_flight = True
        if condition in ('unknown', 'rejected'):
            db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=job.revision, state=condition))
        if condition == 'orphan': db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=job.revision, state='identified', event_id='late', needs_review=True))
    response = client.delete(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == (204 if condition in ('clean', 'rejected') else 409), response.text
    with sessions() as db:
        assert (db.get(models.Booking, booking_id) is None) == (condition in ('clean', 'rejected'))
        job = db.scalar(select(TeamsSyncJob))
        assert job.revision == 1
        assert job.booking_id == (None if condition in ('clean', 'rejected') else booking_id)


@pytest.mark.parametrize('route', ['class', 'user'])
def test_physical_delete_detaches_job_before_booking_id_is_reused(ctx, route):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        booking_id = add_booking(db, user)
        old = db.scalar(select(TeamsSyncJob))
        old.desired = 'delete'
        old.applied_revision = old.revision
        old_id = old.id
    path = f'/admin/classes/{booking_id}' if route == 'class' else f'/admin/users/{user}'
    assert client.delete(path, headers={'Authorization': f'Bearer {create_token(admin)}'}).status_code == 204
    with sessions.begin() as db:
        new_id = add_booking(db, admin, booking_id=booking_id)
        assert new_id == booking_id
        jobs = db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.id)).all()
        assert len(jobs) == 2
        old, new = jobs
        assert (old.id, old.booking_id, old.desired, old.event_id) == (old_id, None, 'delete', None)
        assert old.booking_key.startswith('deleted:')
        assert (new.booking_id, new.booking_key, new.desired, new.event_id) == (new_id, f'booking:{new_id}', 'upsert', None)


def test_delete_class_preserves_existing_job_when_disconnected(ctx, monkeypatch):
    sessions, client, (admin, user) = ctx
    monkeypatch.setattr(main.microsoft_oauth_routes, 'get_config', lambda: None)
    with sessions.begin() as db:
        booking_id = add_booking(db, user)
    response = client.delete(f'/admin/classes/{booking_id}', headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == 409, response.text
    with sessions() as db:
        assert db.get(models.Booking, booking_id) is not None
        job = db.scalar(select(TeamsSyncJob))
        assert (job.booking_id, job.desired, job.revision) == (booking_id, 'upsert', 1)


def test_delete_user_preserves_jobs_and_ignores_legacy(ctx):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        ids = [add_booking(db, user), add_booking(db, user, job=False), add_booking(db, user)]
    response = client.delete(f'/admin/users/{user}', headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == 409, response.text
    with sessions() as db:
        assert db.get(models.User, user) is not None
        assert all(db.get(models.Booking, bid) is not None for bid in ids)
        jobs = db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.id)).all()
        assert [(j.booking_id, j.desired, j.revision) for j in jobs] == [(ids[0], 'upsert', 1), (ids[2], 'upsert', 1)]


def test_api_startup_runs_application_schema_first(monkeypatch):
    import asyncio
    called = []
    monkeypatch.setattr(main.Base.metadata, 'create_all', lambda *_: None)
    monkeypatch.setattr(main, 'ensure_application_schema', lambda engine: called.append('schema'), raising=False)
    for name in ('ensure_booking_management_column', 'ensure_notification_lease_column', 'ensure_public_resource_columns',
                 'ensure_user_avatar_columns', 'ensure_booking_history_column', 'ensure_guest_booking_columns',
                 'ensure_booking_comment_column', 'ensure_class_attachment_column', 'ensure_folder_color_column',
                 'ensure_app_setting_text_column', 'ensure_default_theme', 'seed_admin', 'ensure_default_folder'):
        monkeypatch.setattr(main, name, lambda: called.append('other'))
    async def startup():
        async with main.lifespan(main.app):
            pass
    asyncio.run(startup())
    assert called[0] == 'schema'


@pytest.mark.parametrize('route', ['class', 'user'])
def test_delete_detach_failure_rolls_back(ctx, monkeypatch, route):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        ids = [add_booking(db, user), add_booking(db, user)]
        for job in db.scalars(select(TeamsSyncJob)):
            job.desired = 'delete'; job.applied_revision = job.revision
    original = main.detach_deleted_booking
    calls = []
    def fail(db, booking):
        calls.append(booking.id)
        if route == 'class' or len(calls) == 2:
            raise RuntimeError('injected failure')
        return original(db, booking)
    monkeypatch.setattr(main, 'detach_deleted_booking', fail)
    path = f'/admin/classes/{ids[0]}' if route == 'class' else f'/admin/users/{user}'
    assert client.delete(path, headers={'Authorization': f'Bearer {create_token(admin)}'}).status_code == 500
    with sessions() as db:
        assert db.get(models.User, user) is not None
        assert all(db.get(models.Booking, bid) is not None for bid in ids)
        assert [(j.booking_id, j.booking_key, j.desired, j.revision) for j in db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.booking_id))] == [(bid, f'booking:{bid}', 'delete', 1) for bid in ids]
    if route == 'user':
        assert calls == ids


def test_delete_legacy_class_and_user_without_jobs(ctx):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        booking = add_booking(db, user, job=False)
    headers = {'Authorization': f'Bearer {create_token(admin)}'}
    assert client.delete(f'/admin/classes/{booking}', headers=headers).status_code == 204
    with sessions.begin() as db:
        remaining = add_booking(db, user, job=False)
    assert client.delete(f'/admin/users/{user}', headers=headers).status_code == 204
    with sessions() as db:
        assert db.get(models.Booking, remaining) is None
        assert db.scalar(select(TeamsSyncJob)) is None


def test_delete_past_class_restriction_keeps_job(ctx):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        booking = add_booking(db, user, days=-1)
    assert client.delete(f'/admin/classes/{booking}', headers={'Authorization': f'Bearer {create_token(admin)}'}).status_code == 400
    with sessions() as db:
        assert db.get(models.Booking, booking) is not None
        job = db.scalar(select(TeamsSyncJob))
        assert (job.desired, job.revision) == ('upsert', 1)


def test_postgres_user_delete_blocks_new_fk_booking_and_class_writers(monkeypatch, postgres_test_db):
    import threading
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, IntegrityError
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    entered, release = threading.Event(), threading.Event()
    errors = []
    try:
        with sessions.begin() as db:
            admin = models.User(name='Admin', email='pg-admin@example.com', password_hash='x', role='admin')
            user = models.User(name='Student', email='pg-student@example.com', password_hash='x', role='student')
            db.add_all([admin, user]); db.flush()
            admin_id, user_id = admin.id, user.id
            booking_id = add_booking(db, user_id)
            job = db.scalar(select(TeamsSyncJob))
            job.desired = 'delete'; job.applied_revision = job.revision
        def database():
            with sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = database
        original = main.detach_deleted_booking
        def pause(db, booking):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('timed out waiting for test')
            return original(db, booking)
        monkeypatch.setattr(main, 'detach_deleted_booking', pause)
        client = TestClient(main.app, raise_server_exceptions=False)
        def delete_user():
            try:
                result = client.delete(f'/admin/users/{user_id}', headers={'Authorization': f'Bearer {create_token(admin_id)}'})
                if result.status_code != 204:
                    errors.append(result.text)
            except Exception as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=delete_user)
        thread.start()
        assert entered.wait(10)
        with sessions() as db:
            db.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError) as blocked:
                add_booking(db, user_id, job=False)
            assert blocked.value.orig.pgcode == '55P03'
            db.rollback()
        with sessions() as db:
            db.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError) as blocked:
                db.scalar(select(models.Booking).where(models.Booking.id == booking_id).with_for_update())
            assert blocked.value.orig.pgcode == '55P03'
            db.rollback()
        release.set(); thread.join(10)
        assert not thread.is_alive() and not errors
        with sessions() as db:
            assert db.get(models.User, user_id) is None
            assert db.get(models.Booking, booking_id) is None
            assert db.scalar(select(TeamsSyncJob)).desired == 'delete'
            with pytest.raises(IntegrityError):
                add_booking(db, user_id, job=False)
            db.rollback()
        client.close()
    finally:
        release.set()
        if 'thread' in locals():
            thread.join(10)
        main.app.dependency_overrides.clear()
        engine.dispose()
