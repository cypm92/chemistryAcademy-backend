from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
import httpx
from sqlalchemy import select
from app import models
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending
from test_teams_postgres import sessions


def test_missing_reconciliation_preserves_concurrent_comment(sessions):
    start = datetime.now(timezone.utc) + timedelta(days=10)
    with sessions.begin() as db:
        booking = models.Booking(guest_name='Ana', guest_email='a@example.com', status='confirmed', starts_at=start, ends_at=start + timedelta(hours=1), admin_comment='Manual\nEnlace de Teams: https://example.test/join')
        db.add(booking); db.flush(); bid = booking.id
        enqueue_booking(db, booking, 'upsert')
        job = db.scalar(select(TeamsSyncJob))
        job.event_id, job.join_url, job.applied_revision = 'event', 'https://example.test/join', job.revision
        seed_identified_event(db, job)
        job.last_error = 'manual_reconcile'
    entered, release = Event(), Event()
    errors = []
    def graph(request):
        entered.set()
        assert release.wait(15)
        return httpx.Response(404)
    def worker():
        try:
            with httpx.Client(transport=httpx.MockTransport(graph)) as client:
                process_pending(sessions, lambda: IDENTITY, client)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=worker); thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            booking = db.scalar(select(models.Booking).where(models.Booking.id == bid).with_for_update())
            booking.admin_comment += '\nNueva nota manual'
            enqueue_booking(db, booking, 'upsert')
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.last_error == 'event_missing' and job.event_id == 'event'
        assert db.get(models.Booking, bid).admin_comment == 'Manual\nNueva nota manual'
