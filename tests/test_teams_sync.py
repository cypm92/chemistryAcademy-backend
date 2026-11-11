from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from types import SimpleNamespace
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, merge_teams_comment


def test_new_titles_omit_email_for_guests_and_registered_students():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as db:
        seed_account(db)
        for booking in (
            SimpleNamespace(id=101, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='ana@example.org', user=None),
            SimpleNamespace(id=102, starts_at=datetime(2026, 7, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 7, 1, 13, tzinfo=timezone.utc), guest_name=None, guest_email=None, user=SimpleNamespace(name='Luis', email='luis@example.org')),
        ):
            enqueue_booking(db, booking, 'upsert')
    with Session() as db:
        jobs = {job.booking_key: job.subject for job in db.scalars(select(TeamsSyncJob))}
        assert jobs['booking:101'] == 'Ana_01-01-2026 13:00'
        assert jobs['booking:102'] == 'Luis_01-07-2026 14:00'


def test_comment_replaces_only_old_link_and_preserves_user_text():
    old = 'https://old.example/x'
    new = 'https://new.example/y'
    text = f'Antes {old}\nEnlace de Teams: https://manual.example/x\nEnlace de Teams: {old}\nDespués'
    assert merge_teams_comment(text, old, new) == f'Antes {old}\nEnlace de Teams: https://manual.example/x\nEnlace de Teams: {new}\nDespués'
    assert merge_teams_comment(text, old, None) == f'Antes {old}\nEnlace de Teams: https://manual.example/x\nDespués'
    assert merge_teams_comment(f'Manual {new}', None, new) == f'Manual {new}\nEnlace de Teams: {new}'
    assert merge_teams_comment('Enlace de Teams: https://attacker.example/x', old, None) == 'Enlace de Teams: https://attacker.example/x'


def test_enqueue_is_transactional_and_increments_revision():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as db:
        seed_account(db)
    booking = SimpleNamespace(id=9, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with Session.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    with Session.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        transaction_id = job.transaction_id
        assert job.revision == 1
        assert job.desired == 'upsert'
        enqueue_booking(db, booking, 'delete')
    with Session() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.revision == 2
        assert job.desired == 'delete'
        assert job.transaction_id == transaction_id
        enqueue_booking(db, booking, 'upsert')
        db.rollback()
    with Session() as db:
        assert db.scalar(select(TeamsSyncJob)).desired == 'delete'



def test_worker_creates_event_and_persists_identity():
    import httpx
    from app.teams_sync import process_pending

    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as db:
        seed_account(db)
    booking = SimpleNamespace(id=42, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with Session.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    def respond(request):
        import json
        assert request.method == 'POST'
        assert json.loads(request.content)['subject'] == 'Ana_01-01-2026 13:00'
        return httpx.Response(201, json={'id': 'event-1', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        process_pending(Session, lambda: IDENTITY, client)
    with Session() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.event_id == 'event-1'
        assert job.join_url == 'https://teams.microsoft.com/meeting'
        assert job.applied_revision == job.revision



def test_failure_is_sanitized_and_delayed():
    import httpx
    from app.teams_sync import process_pending
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as db:
        seed_account(db)
    booking = SimpleNamespace(id=10, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name='Ana', guest_email='a@example.org', user=None)
    with Session.begin() as db:
        enqueue_booking(db, booking, 'upsert')
    def bad_token():
        raise ValueError('secret-token')
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        assert process_pending(Session, bad_token, client)
        assert not process_pending(Session, bad_token, client)
    with Session() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.attempts == 1
        assert job.last_error == 'worker_error'
        assert job.lease_token is None


def test_existing_event_without_url_recovers_using_get(monkeypatch):
    import httpx
    from app import microsoft_graph as graph
    from app.teams_models import utcnow
    from app.teams_sync import process_pending
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as db:
        seed_account(db)
    booking = SimpleNamespace(id=51, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc), guest_name="Ana", guest_email="a@example.org", user=None)
    with Session.begin() as db:
        enqueue_booking(db, booking, "upsert")
        job = db.scalar(select(TeamsSyncJob))
        job.event_id = "existing"
        seed_identified_event(db, job)
        job.available_at = utcnow()
    calls = []
    monkeypatch.setattr(graph, "get_event", lambda client, token, event_id: calls.append(event_id) or "https://teams.microsoft.com/l/meetup-join/x", raising=False)
    monkeypatch.setattr(graph, "update_event", lambda *args: calls.append("patched") or None)
    with httpx.Client() as client:
        process_pending(Session, lambda: IDENTITY, client)
    with Session() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert calls == ["existing", "patched"]
        assert job.join_url == "https://teams.microsoft.com/l/meetup-join/x"
        assert job.applied_revision == job.revision
