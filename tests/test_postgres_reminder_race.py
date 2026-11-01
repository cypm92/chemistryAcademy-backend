"""PostgreSQL race regression; run only against a disposable, isolated database."""
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.database import Base
from app.main import update_admin_class, cancel_admin_class
from app.notifications import enqueue_due_reminders, deliver_pending


@unittest.skipUnless(os.getenv('ISOLATED_POSTGRES_TEST_URL'), 'requires disposable PostgreSQL')
class ReminderRaceTest(unittest.TestCase):
    def test_edit_between_booking_read_and_generation_lookup(self):
        engine = create_engine(os.environ['ISOLATED_POSTGRES_TEST_URL'])
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False)
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        with sessions() as db:
            booking = models.Booking(guest_name='Race', guest_email='race@example.com', starts_at=start,
                                     ends_at=start + timedelta(minutes=30), status='confirmed', note='Química')
            db.add(booking); db.commit(); booking_id = booking.id
        reached = threading.Event(); release = threading.Event(); finished = threading.Event(); errors = []
        def before_sql(conn, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name == 'scheduler' and 'notification_events.logical_key' in statement and 'ORDER BY' in statement:
                reached.set()
                if not release.wait(10):
                    raise RuntimeError('scheduler barrier timed out')
        event.listen(engine, 'before_cursor_execute', before_sql)
        def schedule():
            try:
                with patch('app.notifications.datetime', wraps=datetime) as clock:
                    clock.now.return_value = start - timedelta(hours=2)
                    enqueue_due_reminders(sessions)
            except Exception as exc:
                errors.append(exc)
        def edit():
            try:
                with sessions() as db:
                    update_admin_class(booking_id, schemas.AdminClassUpdate(starts_at=start, duration_slots=2,
                                       subject='Física ESO', admin_comment=''), None, db)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        scheduler = threading.Thread(target=schedule, name='scheduler'); scheduler.start()
        try:
            self.assertTrue(reached.wait(10), 'scheduler did not reach generation lookup')
            editor = threading.Thread(target=edit); editor.start()
            finished.wait(1)
        finally:
            release.set()
        scheduler.join(10); editor.join(10)
        self.assertFalse(scheduler.is_alive() or editor.is_alive(), 'threads did not finish')
        self.assertEqual(errors, [])
        with patch('app.notifications.datetime', wraps=datetime) as clock:
            clock.now.return_value = start - timedelta(hours=2)
            enqueue_due_reminders(sessions)
        with sessions() as db:
            reminders = list(db.scalars(select(models.NotificationEvent).where(
                models.NotificationEvent.subject == 'Recordatorio de tu clase', models.NotificationEvent.status == 'pending')))
            self.assertEqual(len(reminders), 1)
            self.assertIn('Física', reminders[0].body)
        engine.dispose()

    def test_cancellation_during_scheduling_invalidates_reminder(self):
        engine = create_engine(os.environ['ISOLATED_POSTGRES_TEST_URL'])
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False)
        start = datetime.now(timezone.utc) + timedelta(hours=6)
        with sessions() as db:
            booking = models.Booking(guest_name='Cancel', guest_email='cancel@example.com', starts_at=start,
                                     ends_at=start + timedelta(minutes=30), status='confirmed', note='Química')
            db.add(booking); db.commit(); booking_id = booking.id
        reached = threading.Event(); release = threading.Event(); errors = []
        def before_sql(conn, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name == 'scheduler' and 'notification_events.logical_key' in statement and 'ORDER BY' in statement:
                reached.set()
                if not release.wait(10):
                    raise RuntimeError('scheduler barrier timed out')
        event.listen(engine, 'before_cursor_execute', before_sql)
        def schedule():
            try:
                enqueue_due_reminders(sessions)
            except Exception as exc:
                errors.append(exc)
        def cancel():
            try:
                with sessions() as db:
                    cancel_admin_class(booking_id, None, db)
            except Exception as exc:
                errors.append(exc)
        scheduler = threading.Thread(target=schedule, name='scheduler'); scheduler.start()
        try:
            self.assertTrue(reached.wait(10))
            cancellation = threading.Thread(target=cancel); cancellation.start()
        finally:
            release.set()
        scheduler.join(10); cancellation.join(10)
        self.assertFalse(scheduler.is_alive() or cancellation.is_alive())
        self.assertEqual(errors, [])
        with sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent).where(
                models.NotificationEvent.logical_key.like(f'booking:{booking_id}:%'))))
            self.assertFalse(any(e.status == 'pending' and ':reminder:' in e.logical_key for e in events))
            self.assertTrue(any(e.status == 'pending' and e.subject == 'Clase cancelada' for e in events))
        engine.dispose()

    def test_cancellation_does_not_wait_for_inflight_smtp(self):
        engine = create_engine(os.environ['ISOLATED_POSTGRES_TEST_URL'])
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False)
        start = datetime.now(timezone.utc) + timedelta(hours=4)
        with sessions() as db:
            booking = models.Booking(guest_name='SMTP', guest_email='smtp@example.com', starts_at=start,
                                     ends_at=start + timedelta(minutes=30), status='confirmed', note='Química')
            db.add(booking); db.commit(); booking_id = booking.id
        enqueue_due_reminders(sessions)
        entered = threading.Event(); release = threading.Event(); done = threading.Event(); errors = []
        def slow_send(event):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('SMTP barrier timed out')
            return True
        def deliver():
            try:
                deliver_pending(sessions)
            except Exception as exc:
                errors.append(exc)
        def cancel():
            try:
                with sessions() as db:
                    cancel_admin_class(booking_id, None, db)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        with patch('app.notifications._send', side_effect=slow_send):
            worker = threading.Thread(target=deliver); worker.start()
            try:
                self.assertTrue(entered.wait(10))
                cancellation = threading.Thread(target=cancel); cancellation.start()
                self.assertTrue(done.wait(3), 'cancellation blocked by SMTP')
            finally:
                release.set()
                worker.join(10)
            cancellation.join(10)
        self.assertEqual(errors, [])
        with sessions() as db:
            reminder = db.scalar(select(models.NotificationEvent).where(
                models.NotificationEvent.logical_key.like(f'booking:{booking_id}:reminder:%')))
            self.assertEqual(reminder.status, 'cancelled')
            self.assertIsNotNone(reminder.sent_at)
        engine.dispose()
