"""Real booking races against a dedicated ephemeral PostgreSQL server."""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from fastapi import HTTPException
from sqlalchemy import event, select, text
from ephemeral_postgres import ephemeral_postgres
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.database import Base
from app.main import (create_admin_class, request_booking, request_public_trial_booking,
                      update_admin_class, update_booking_status)


class RealBookingRaceTest(unittest.TestCase):
    def setUp(self):
        context = ephemeral_postgres()
        self.postgres = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.engine = self.postgres.engine
        self.control = self.engine
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            student = models.User(name='Student', email='student@example.com', password_hash='x', role='guest')
            admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
            db.add_all([student, admin]); db.commit()
            self.student_id, self.admin_id = student.id, admin.id
        self.admin = SimpleNamespace(id=self.admin_id, role='admin')
        start = (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.start = start + timedelta(days=(-start.weekday()) % 7)

    def tearDown(self):
        self.engine.dispose()

    def race(self, first_operation, second_operation):
        reached, release, attempting = threading.Event(), threading.Event(), threading.Event()
        outcomes, pids = {}, {}
        def after_sql(conn, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name == 'first' and 'bookings.starts_at <' in statement:
                reached.set()
                if not release.wait(15):
                    raise RuntimeError('first transaction barrier timed out')
        def before_sql(conn, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name == 'second' and 'pg_advisory_xact_lock' in statement:
                pids['second'] = conn.connection.driver_connection.get_backend_pid()
                attempting.set()
        event.listen(self.engine, 'after_cursor_execute', after_sql)
        event.listen(self.engine, 'before_cursor_execute', before_sql)
        def run(name, operation):
            try:
                with self.sessions() as db:
                    outcomes[name] = ('ok', operation(db))
            except HTTPException as exc:
                outcomes[name] = ('http', exc.status_code)
            except Exception as exc:
                outcomes[name] = ('error', repr(exc))
        first = threading.Thread(target=run, args=('first', first_operation), name='first')
        second = threading.Thread(target=run, args=('second', second_operation), name='second')
        first.start()
        try:
            self.assertTrue(reached.wait(10), f'first did not reach conflict check: {outcomes}')
            second.start()
            self.assertTrue(attempting.wait(10), f'second did not reach lock: {outcomes}')
            deadline = time.monotonic() + 5
            blocked = False
            with self.control.connect() as conn:
                while time.monotonic() < deadline:
                    blocked = bool(conn.scalar(text('SELECT cardinality(pg_blocking_pids(:pid))'), {'pid': pids['second']}))
                    if blocked:
                        break
                    time.sleep(0.02)
            self.assertTrue(blocked, f'second transaction never blocked: {outcomes}')
        finally:
            release.set()
            first.join(15)
            if second.ident is not None:
                second.join(15)
            event.remove(self.engine, 'after_cursor_execute', after_sql)
            event.remove(self.engine, 'before_cursor_execute', before_sql)
        self.assertFalse(first.is_alive() or second.is_alive())
        return outcomes

    def public(self, db):
        return request_public_trial_booking(schemas.PublicTrialBookingCreate(
            name='Guest', email='guest@example.com', starts_at=self.start, subject='Química ESO'), db)

    def registered(self, db):
        student = db.get(models.User, self.student_id)
        return request_booking(schemas.BookingCreate(starts_at=self.start + timedelta(minutes=30),
                               subject='Química ESO', duration_slots=2), student, db)

    def admin_create(self, db):
        return create_admin_class(schemas.AdminBookingCreate(user_id=self.student_id, starts_at=self.start,
                                  subject='Química ESO', duration_slots=2), self.admin, db)

    def assert_one_active(self, status):
        with self.sessions() as db:
            rows = list(db.scalars(select(models.Booking).where(
                models.Booking.starts_at < self.start + timedelta(hours=1),
                models.Booking.ends_at > self.start,
                models.Booking.status.in_(['requested', 'confirmed', 'blocked', 'unavailable']))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, status)
            return rows[0].id

    def test_public_and_registered_requests_both_orderings(self):
        for first, second in ((self.public, self.registered), (self.registered, self.public)):
            with self.subTest(first=first.__name__):
                result = self.race(first, second)
                self.assertEqual(result['first'][0], 'ok', result)
                self.assertEqual(result['second'], ('http', 409), result)
                booking_id = self.assert_one_active('requested')
                with self.sessions() as db:
                    booking = db.get(models.Booking, booking_id)
                    self.assertEqual(booking.user_id is None, first == self.public)
                    db.delete(booking); db.commit()

    def test_admin_create_and_edit_both_orderings(self):
        for edit_first in (True, False):
            with self.subTest(edit_first=edit_first):
                with self.sessions() as db:
                    booking = models.Booking(user_id=self.student_id, starts_at=self.start + timedelta(hours=3),
                                             ends_at=self.start + timedelta(hours=4), status='confirmed', note='Química ESO')
                    db.add(booking); db.commit(); booking_id = booking.id
                def edit(db):
                    return update_admin_class(booking_id, schemas.AdminClassUpdate(starts_at=self.start + timedelta(minutes=30),
                                              duration_slots=2, subject='Química ESO'), self.admin, db)
                result = self.race(edit if edit_first else self.admin_create,
                                   self.admin_create if edit_first else edit)
                self.assertEqual(result['first'][0], 'ok', result)
                self.assertEqual(result['second'], ('http', 409), result)
                winner = self.assert_one_active('confirmed')
                with self.sessions() as db:
                    edited = db.get(models.Booking, booking_id)
                    self.assertEqual(edited.starts_at, self.start + timedelta(minutes=30) if edit_first else self.start + timedelta(hours=3))
                    for row in db.scalars(select(models.Booking)):
                        db.delete(row)
                    db.commit()

    def test_confirm_and_overlapping_admin_create(self):
        for confirm_first in (True, False):
            with self.subTest(confirm_first=confirm_first):
                with self.sessions() as db:
                    booking = models.Booking(user_id=self.student_id, starts_at=self.start,
                                             ends_at=self.start + timedelta(hours=1), status='requested', note='Química ESO')
                    db.add(booking); db.commit(); booking_id = booking.id
                def confirm(db):
                    return update_booking_status(booking_id, schemas.BookingStatusUpdate(status='confirmed'), self.admin, db)
                result = self.race(confirm if confirm_first else self.admin_create,
                                   self.admin_create if confirm_first else confirm)
                self.assertEqual(result['first'][0] if confirm_first else result['second'][0], 'ok', result)
                self.assertEqual(result['second'] if confirm_first else result['first'], ('http', 409), result)
                self.assertEqual(self.assert_one_active('confirmed'), booking_id)
                with self.sessions() as db:
                    db.delete(db.get(models.Booking, booking_id)); db.commit()
