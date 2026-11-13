import threading
import unittest
from datetime import datetime, timedelta, timezone
from ephemeral_postgres import ephemeral_postgres


class PostgresReportRaceTest(unittest.TestCase):
    def setUp(self):
        context = ephemeral_postgres()
        self.postgres = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_concurrent_workers_insert_one_snapshot(self):
        from sqlalchemy import select
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models import Booking, NotificationEvent
        from app.admin_reports import enqueue_due_reports
        engine = self.postgres.engine
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        now = datetime(2026, 5, 5, 8, tzinfo=timezone.utc)
        end = now - timedelta(days=2)
        with sessions.begin() as db:
            db.add(Booking(guest_name='Race', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
        barrier = threading.Barrier(2)
        errors = []
        def run():
            try:
                barrier.wait(timeout=10)
                enqueue_due_reports(sessions, now)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        with sessions() as db:
            events = list(db.scalars(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:daily:2026-05-05:%'))))
            self.assertEqual(len(events), 1)
            monthly = list(db.scalars(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:monthly:2026-04:%'))))
            self.assertEqual(len(monthly), 1)
        # Retrying after both workers complete must preserve the original snapshots.
        original = {event.logical_key: event.body for event in events + monthly}
        enqueue_due_reports(sessions, now + timedelta(hours=1))
        with sessions() as db:
            for key, body in original.items():
                self.assertEqual(db.scalar(select(NotificationEvent).where(NotificationEvent.logical_key == key)).body, body)
        engine.dispose()
