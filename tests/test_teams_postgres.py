"""Concurrency tests require an explicitly provided disposable PostgreSQL database."""
from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from uuid import uuid4
from app.database import Base
from app import models
from sqlalchemy.orm import sessionmaker

from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending


@pytest.fixture
def sessions():
    url = os.environ.get('TEAMS_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('TEAMS_TEST_POSTGRES_URL must name an isolated test database')
    if not url.startswith(('postgresql://', 'postgresql+psycopg2://')):
        pytest.fail('PostgreSQL required')
    admin = create_engine(url)
    schema = 'teams_test_' + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        seed_account(db)
    yield factory
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


def test_create_without_join_url_remains_pending(sessions):
    booking = SimpleNamespace(id=42, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={'id': 'event-1'}))) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == 'event-1'
        assert job.applied_revision < job.revision


def test_late_create_after_cancel_reopens_delete(sessions):
    from threading import Event, Thread
    from datetime import timedelta
    from app.teams_models import utcnow
    booking = SimpleNamespace(id=43, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name="Ana", guest_email="a@example.org", user=None)
    entered, release = Event(), Event()
    errors, remote, calls = [], set(), []
    def respond(request):
        calls.append(request.method)
        assert request.method == "POST"
        entered.set()
        assert release.wait(10)
        remote.add("late-event")
        return httpx.Response(201, json={"id": "late-event", "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"}})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    with sessions.begin() as db:
        enqueue_booking(db, booking, "upsert")
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            enqueue_booking(db, booking, "delete")
            job = db.scalar(select(TeamsSyncJob))
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request.method))) as client:
            assert not process_pending(sessions, lambda: IDENTITY, client)
        assert calls == ["POST"]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == "late-event"
        assert job.applied_revision < job.revision
        assert job.join_url is None
    def delete(request):
        calls.append(request.method)
        assert request.method == "DELETE" and request.url.path.endswith('/late-event')
        remote.remove('late-event')
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(delete)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id is None and job.applied_revision == job.revision
    assert calls == ['POST', 'DELETE'] and not remote


def test_missing_join_url_is_recovered_without_another_create(sessions, monkeypatch):
    from app import microsoft_graph as graph
    from app.teams_models import utcnow
    booking = SimpleNamespace(id=52, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name="Ana", guest_email="a@example.org", user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, "upsert")
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(201, json={"id": "event-52"}))) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.available_at = utcnow()
    calls = []
    monkeypatch.setattr(graph, "get_event", lambda client, token, event_id: calls.append(event_id) or "https://teams.microsoft.com/l/meetup-join/x", raising=False)
    monkeypatch.setattr(graph, "update_event", lambda *args: None)
    with httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("unexpected HTTP request")))) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert calls == ["event-52"]
        assert job.join_url == "https://teams.microsoft.com/l/meetup-join/x"
        assert job.applied_revision == job.revision


def test_expired_post_collisions_are_durably_cleaned_without_touching_winner(sessions):
    from datetime import timedelta
    from threading import Event, Thread
    from app.teams_models import utcnow

    booking = SimpleNamespace(id=73, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    entered, release = Event(), Event()
    errors, posts, deletes = [], [], []
    def create(request):
        posts.append(request.method)
        entered.set()
        assert release.wait(20)
        return httpx.Response(201, json={'id': 'winner', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/winner'}})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(create)) as client:
                assert process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob))
            assert job.creation_in_flight
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        with httpx.Client(transport=httpx.MockTransport(lambda req: posts.append(req.method))) as client:
            # The first expired claim records uncertainty; later attempts cannot claim it.
            assert process_pending(sessions, lambda: IDENTITY, client)
            for _ in range(3):
                assert not process_pending(sessions, lambda: IDENTITY, client)
        with sessions() as db:
            job = db.scalar(select(TeamsSyncJob))
            assert job.creation_in_flight and job.last_error == 'creation_result_unknown'
        assert posts == ['POST']
    finally:
        release.set(); thread.join(20)
    assert not thread.is_alive() and not errors
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == 'winner' and not job.creation_in_flight
        # Legacy collisions may predate the fail-closed rule.
        job.join_url = 'https://teams.microsoft.com/winner'
        job.applied_revision = job.revision
        db.execute(text('CREATE TABLE teams_orphan_events (job_id INTEGER, event_id TEXT, completed_at TIMESTAMP)'))
        for event_id in ('winner', 'orphan-0', 'orphan-1'):
            db.execute(text('INSERT INTO teams_orphan_events VALUES (:job, :event, NULL)'), {'job': job.id, 'event': event_id})
    def forbidden(request):
        raise AssertionError(f'unexpected Graph request: {request.method}')
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        for _ in range  (5):
            process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.scalar(select(TeamsSyncJob)).event_id == 'winner'
        orphans = db.execute(text('SELECT event_id, completed_at FROM teams_orphan_events')).all()
        assert {o.event_id for o in orphans} == {'winner', 'orphan-0', 'orphan-1'}
        assert all(o.completed_at is None for o in orphans)


def test_legacy_cleanup_never_deletes_linked_or_unlinked_event(sessions):
    booking = SimpleNamespace(id=74, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = 'protected'; job.applied_revision = job.revision
        db.execute(text('CREATE TABLE teams_orphan_events (job_id INTEGER, event_id TEXT, completed_at TIMESTAMP, attempts INTEGER)'))
        db.execute(text("INSERT INTO teams_orphan_events VALUES (:job, 'protected', NULL, 0)"), {'job': job.id})
    def forbidden(request):
        raise AssertionError(f'unexpected Graph request: {request.method}')
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        process_pending(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            db.scalar(select(TeamsSyncJob)).event_id = 'other'
        process_pending(sessions, lambda: None, client)
        process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        orphan = db.execute(text('SELECT event_id, completed_at, attempts FROM teams_orphan_events')).one()
        assert orphan == ('protected', None, 0)


def test_completed_legacy_event_remains_unchanged_after_creation(sessions):
    booking = SimpleNamespace(id=75, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        db.execute(text('CREATE TABLE teams_orphan_events (job_id INTEGER, event_id TEXT, completed_at TIMESTAMP)'))
        db.execute(text("INSERT INTO teams_orphan_events VALUES (:job, 'again', '2020-01-01')"), {'job': job.id})
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={'id': 'again'}))) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.execute(text("SELECT count(*) FROM teams_orphan_events WHERE event_id = 'again' AND completed_at IS NOT NULL")).scalar_one() == 1


def test_comment_lifecycle_preserves_manual_text(sessions, monkeypatch):
    from app import microsoft_graph as graph
    old, new = 'https://teams.microsoft.com/old', 'https://teams.microsoft.com/new'
    with sessions.begin() as db:
        booking = models.Booking(starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), status='confirmed',
            guest_name='Ana', guest_email='a@example.org', admin_comment=f'Manual {old}\nEnlace de Teams: https://evil.example/')
        db.add(booking)
        db.flush()
        booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    monkeypatch.setattr(graph, 'create_event', lambda *args: ('event', old))
    monkeypatch.setattr(graph, 'update_event', lambda *args: new)
    monkeypatch.setattr(graph, 'delete_event', lambda *args: None)
    with httpx.Client() as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            booking = db.get(models.Booking, booking_id)
            assert booking.admin_comment == f'Manual {old}\nEnlace de Teams: https://evil.example/\nEnlace de Teams: {old}'
            enqueue_booking(db, booking, 'upsert')
        assert process_pending(sessions, lambda: IDENTITY, client)
        with sessions.begin() as db:
            booking = db.get(models.Booking, booking_id)
            assert booking.admin_comment.endswith(f'Enlace de Teams: {new}')
            booking.status = 'cancelled'
            enqueue_booking(db, booking, 'delete')
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == f'Manual {old}\nEnlace de Teams: https://evil.example/'


def test_unconfirmed_booking_does_not_receive_link(sessions, monkeypatch):
    from app import microsoft_graph as graph
    with sessions.begin() as db:
        booking = models.Booking(starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), status='cancelled',
            guest_name='Ana', guest_email='a@example.org', admin_comment='Manual')
        db.add(booking)
        db.flush()
        booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    monkeypatch.setattr(graph, 'create_event', lambda *args: ('event', 'https://teams.microsoft.com/x'))
    with httpx.Client() as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == 'Manual'


def test_comment_editor_and_worker_serialize_without_deadlock(sessions, monkeypatch):
    from threading import Event, Thread
    from app import microsoft_graph as graph
    url = 'https://teams.microsoft.com/x'
    with sessions.begin() as db:
        booking = models.Booking(starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), status='confirmed',
            guest_name='Ana', guest_email='a@example.org', admin_comment='Original')
        db.add(booking); db.flush()
        booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    returned, release = Event(), Event()
    errors = []
    def create(*args):
        returned.set()
        assert release.wait(10)
        return 'event', url
    monkeypatch.setattr(graph, 'create_event', create)
    def worker():
        try:
            with httpx.Client() as client:
                assert process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert returned.wait(10)
        with sessions.begin() as db:
            booking = db.scalar(select(models.Booking).where(models.Booking.id == booking_id).with_for_update())
            booking.admin_comment = 'Editado por profesora'
            release.set()
            # A concurrent enqueue takes the job lock while holding the booking lock.
            enqueue_booking(db, booking, 'upsert')
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == 'Editado por profesora'
    monkeypatch.setattr(graph, 'update_event', lambda *args: url)
    monkeypatch.setattr(graph, 'get_event', lambda *args: url)
    with httpx.Client() as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == f'Editado por profesora\nEnlace de Teams: {url}'


@pytest.mark.parametrize('expired', [False, True])
def test_legacy_cleanup_leases_are_ignored_and_booking_edits_proceed(sessions, expired):
    from datetime import timedelta
    from threading import Barrier, Thread
    from app.teams_models import utcnow
    with sessions.begin() as db:
        booking = models.Booking(starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), status='confirmed',
            guest_name='Ana', guest_email='a@example.org')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob)); job.event_id = 'winner'; job.applied_revision = job.revision
        db.execute(text('CREATE TABLE teams_orphan_events (job_id INTEGER, event_id TEXT, state TEXT, lease_token TEXT, lease_expires_at TIMESTAMP, completed_at TIMESTAMP)'))
        db.execute(text("INSERT INTO teams_orphan_events VALUES (:job, 'orphan', 'deleting', 'legacy-lease', :expiry, NULL)"), {'job': job.id, 'expiry': utcnow() + timedelta(seconds=-1 if expired else 60)})
    barrier = Barrier(2); errors = []; requests = []
    def worker():
        try:
            barrier.wait(10)
            with httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r.method) or httpx.Response(204))) as client:
                process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker); thread.start()
    barrier.wait(10)
    try:
        with sessions.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '500ms'"))
            booking = db.scalar(select(models.Booking).where(models.Booking.id == booking_id).with_for_update())
            booking.admin_comment = 'edited'
    finally:
        thread.join(15)
    assert not thread.is_alive() and not errors and not requests
    with sessions.begin() as db:
        assert db.get(models.Booking, booking_id).admin_comment == 'edited'
        orphan = db.execute(text('SELECT event_id, completed_at FROM teams_orphan_events')).one()
        assert orphan == ('orphan', None)
        db.execute(text('UPDATE teams_orphan_events SET lease_expires_at = :expiry'), {'expiry': utcnow() - timedelta(seconds=1)})
    with httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r.method) or httpx.Response(204))) as client:
        process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.execute(text('SELECT completed_at FROM teams_orphan_events')).scalar_one() is None
        assert db.scalar(select(TeamsSyncJob)).event_id == 'winner'
    assert not requests


def test_cancel_during_create_never_publishes_late_link(sessions, monkeypatch):
    from threading import Event, Thread
    from app import microsoft_graph as graph
    entered, release = Event(), Event()
    errors = []
    with sessions.begin() as db:
        booking = models.Booking(starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), status='confirmed',
            guest_name='Ana', guest_email='a@example.org', admin_comment='Manual')
        db.add(booking); db.flush(); booking_id = booking.id
        enqueue_booking(db, booking, 'upsert')
    def create(*args):
        entered.set(); assert_wait = release.wait(10)
        assert assert_wait
        return 'event', 'https://teams.microsoft.com/late'
    monkeypatch.setattr(graph, 'create_event', create)
    monkeypatch.setattr(graph, 'delete_event', lambda *args: None)
    def worker():
        try:
            with httpx.Client() as client:
                assert process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            booking = db.scalar(select(models.Booking).where(models.Booking.id == booking_id).with_for_update())
            booking.status = 'cancelled'
            enqueue_booking(db, booking, 'delete')
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with httpx.Client() as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        assert db.get(models.Booking, booking_id).admin_comment == 'Manual'
        assert db.scalar(select(TeamsSyncJob)).event_id is None




def test_expired_patch_finishing_last_is_reconciled(sessions):
    from datetime import timedelta
    from threading import Event, Thread
    import json
    from app.teams_models import utcnow

    booking = SimpleNamespace(id=601, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Old', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = 'event'
        job.join_url = 'https://teams.microsoft.com/current'
        seed_identified_event(db, job)
    entered, release = Event(), Event()
    errors, remote = [], {}
    def handler(request):
        assert request.method == 'PATCH'
        payload = json.loads(request.content)
        if payload['subject'].startswith('Old_'):
            entered.set()
            assert release.wait(15)
        remote.update(payload)
        return httpx.Response(200, json={})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                assert process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        booking.guest_name = 'New'
        with sessions.begin() as db:
            enqueue_booking(db, booking, 'upsert')
            db.scalar(select(TeamsSyncJob)).lease_expires_at = utcnow() - timedelta(seconds=1)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert process_pending(sessions, lambda: IDENTITY, client)
        assert remote['subject'].startswith('New_')
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    assert remote['subject'].startswith('Old_')
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert (job.event_id, job.join_url, job.applied_revision) == ('event', 'https://teams.microsoft.com/current', 2)
        assert job.applied_revision < job.revision
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    assert remote['subject'].startswith('New_')
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.applied_revision == job.revision


def test_enqueue_new_revision_clears_previous_error(sessions):
    booking = SimpleNamespace(id=602, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        job.last_error = 'graph_error_500'
        job.attempts = 2
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.revision == 2 and job.attempts == 0 and job.last_error is None


def test_late_patch_response_does_not_resurrect_completed_delete(sessions):
    from datetime import timedelta
    from threading import Event, Thread
    from app.teams_models import utcnow

    booking = SimpleNamespace(id=603, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = 'event'
        job.join_url = 'https://teams.microsoft.com/current'
        seed_identified_event(db, job)
    entered, release = Event(), Event()
    errors = []
    def patch(request):
        assert request.method == 'PATCH'
        entered.set()
        assert release.wait(15)
        return httpx.Response(200, json={})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(patch)) as client:
                assert process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            enqueue_booking(db, booking, 'delete')
            db.scalar(select(TeamsSyncJob)).lease_expires_at = utcnow() - timedelta(seconds=1)
        def delete(request):
            assert request.method == 'DELETE'
            return httpx.Response(204)
        with httpx.Client(transport=httpx.MockTransport(delete)) as client:
            assert process_pending(sessions, lambda: IDENTITY, client)
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert (job.desired, job.event_id, job.join_url, job.revision, job.applied_revision) == ('delete', None, None, 2, 2)


@pytest.mark.parametrize('scenario', ['selected', 'new_lease_cancelled', 'same_event', 'different_account'])
def test_late_creation_preserves_manual_selection_postgres(sessions, scenario):
    from threading import Event, Thread
    from app.microsoft_auth_models import MicrosoftOAuthGeneration
    from app.microsoft_refresh import MicrosoftAccessTokenIdentity
    from app.teams_models import TeamsCreationAttempt

    identity = MicrosoftAccessTokenIdentity('token', 'tenant', 'account', 4)
    booking = SimpleNamespace(id=914, starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 2, tzinfo=timezone.utc), guest_name='Ana',
        guest_email='a@example.org', user=None)
    with sessions.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    entered, release = Event(), Event()
    errors = []
    def respond(request):
        assert request.method == 'POST'
        entered.set()
        assert release.wait(15)
        return httpx.Response(201, json={'id': 'late', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/late'}})
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                assert process_pending(sessions, lambda: identity, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            job = db.scalar(select(TeamsSyncJob).with_for_update())
            job.transaction_id = 'new-selection'
            job.event_id = 'manual'
            job.join_url = 'https://manual'
            if scenario == 'same_event':
                job.event_id = 'late'
            if scenario == 'new_lease_cancelled':
                job.desired = 'delete'
                job.creation_in_flight = True
                job.lease_token = 'new-worker'
            if scenario == 'different_account':
                db.get(MicrosoftOAuthGeneration, 1).account_object_id = 'other-account'
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        attempt = db.scalar(select(TeamsCreationAttempt))
        assert (job.event_id, job.join_url, job.transaction_id) == (('late' if scenario == 'same_event' else 'manual'), 'https://manual', 'new-selection')
        assert (attempt.state, attempt.event_id, attempt.account_object_id, attempt.join_url) == ('identified', 'late', 'account', 'https://teams.microsoft.com/late')
        assert attempt.needs_review is (scenario != 'same_event')
        if scenario == 'new_lease_cancelled':
            assert (job.desired, job.creation_in_flight, job.lease_token) == ('delete', True, 'new-worker')
