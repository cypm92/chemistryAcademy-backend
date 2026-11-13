import pytest
from sqlalchemy import select
from ephemeral_postgres import postgres_test_db

from app import models
from app.security import create_token
from app.teams_models import TeamsSyncJob
from test_teams_delete_hooks import ctx, add_booking


@pytest.mark.parametrize('route', ['class', 'user'])
@pytest.mark.parametrize('uncertain', ['in_flight', 'unknown'])
def test_uncertain_creation_prevents_physical_delete(ctx, route, uncertain):
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        first = add_booking(db, user)
        booking_id = add_booking(db, user)
        booking = db.get(models.Booking, booking_id)
        booking.admin_comment = 'Review with administrator'
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
        job.creation_in_flight = uncertain == 'in_flight'
        job.last_error = 'creation_result_unknown' if uncertain == 'unknown' else None
    path = f'/admin/classes/{booking_id}' if route == 'class' else f'/admin/users/{user}'
    response = client.delete(path, headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == 409, response.text
    assert 'Teams' in response.json()['detail']
    with sessions() as db:
        assert db.get(models.User, user) is not None
        assert db.get(models.Booking, first) is not None
        assert db.get(models.Booking, booking_id).admin_comment == 'Review with administrator'
        jobs = db.scalars(select(TeamsSyncJob).order_by(TeamsSyncJob.booking_id)).all()
        assert [(j.booking_id, j.desired, j.revision) for j in jobs] == [(first, 'upsert', 1), (booking_id, 'upsert', 1)]


@pytest.mark.parametrize('route', ['class', 'user'])
def test_postgres_delete_waits_for_creation_marker(route, postgres_test_db):
    import threading
    import time
    from fastapi.testclient import TestClient
    from sqlalchemy import event, text
    from sqlalchemy.orm import sessionmaker
    from app import main
    from app.database import Base, get_db

    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    entered = threading.Event()
    results = []
    thread = None
    client = None
    try:
        with sessions.begin() as db:
            admin = models.User(name='Admin', email='race-admin@example.com', password_hash='x', role='admin')
            user = models.User(name='Student', email='race-student@example.com', password_hash='x', role='student')
            db.add_all([admin, user]); db.flush()
            admin_id, user_id = admin.id, user.id
            booking_id = add_booking(db, user_id)
        def database():
            with sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = database
        client = TestClient(main.app, raise_server_exceptions=False)
        with sessions() as writer:
            job = writer.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id).with_for_update())
            job.creation_in_flight = True
            writer.flush()
            @event.listens_for(engine, 'before_cursor_execute')
            def observe(conn, cursor, statement, parameters, context, executemany):
                if 'teams_sync_jobs' in statement and 'FOR UPDATE' in statement and threading.current_thread() is not threading.main_thread():
                    entered.set()
            path = f'/admin/classes/{booking_id}' if route == 'class' else f'/admin/users/{user_id}'
            thread = threading.Thread(target=lambda: results.append(client.delete(path, headers={'Authorization': f'Bearer {create_token(admin_id)}'})))
            thread.start()
            assert entered.wait(10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    waiting = observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%teams_sync_jobs%'"))
                if waiting:
                    break
                time.sleep(0.05)
            assert waiting, 'delete did not wait for the job lock'
            writer.commit()
        thread.join(10)
        assert not thread.is_alive()
        assert results[0].status_code == 409, results[0].text
        with sessions() as db:
            assert db.get(models.User, user_id) is not None
            assert db.get(models.Booking, booking_id) is not None
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id))
            assert job.creation_in_flight and job.desired == 'upsert'
    finally:
        if thread is not None:
            thread.join(10)
        if client is not None:
            client.close()
        main.app.dependency_overrides.clear()
        engine.dispose()
