import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from ephemeral_postgres import ephemeral_postgres

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class ClassManagementTest(unittest.TestCase):
    def setUp(self):
        self.postgres = ephemeral_postgres()
        database = self.postgres.__enter__()
        self.addCleanup(self.postgres.__exit__, None, None, None)
        self.engine = database.engine
        Base.metadata.create_all(self.engine)
        self.sessions = database.session_factory
        def test_db():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app)
        with self.sessions() as db:
            admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
            student = models.User(name='Student', email='student@example.com', password_hash='x', role='guest')
            db.add_all([admin, student]); db.commit()
            self.admin, self.student = admin.id, student.id

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear(); self.engine.dispose()

    def headers(self, user):
        return {'Authorization': f'Bearer {create_token(user)}'}

    def booking(self, status='confirmed', end=None, guest=False):
        end = end or datetime.now(timezone.utc) - timedelta(hours=1)
        with self.sessions() as db:
            booking = models.Booking(user_id=None if guest else self.student, guest_name='Guest' if guest else None,
                                     guest_email='guest@example.com' if guest else None,
                                     starts_at=end - timedelta(hours=1), ends_at=end, status=status)
            db.add(booking); db.commit(); return booking.id

    def test_finished_confirmed_class_can_be_marked_and_serialized(self):
        booking_id = self.booking()
        path = f'/admin/classes/{booking_id}/management'
        response = self.client.patch(path, headers=self.headers(self.admin), json={'management_status': 'paid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()['status'], response.json()['management_status']), ('confirmed', 'paid'))
        row = next(row for row in self.client.get('/admin/classes', headers=self.headers(self.admin)).json() if row['id'] == booking_id)
        self.assertEqual(row['management_status'], 'paid')
        row = next(row for row in self.client.get('/classes', headers=self.headers(self.student)).json() if row['id'] == booking_id)
        self.assertIsNone(row.get('management_status'))
        response = self.client.patch(f'/admin/classes/{booking_id}/management', headers=self.headers(self.admin), json={'management_status': 'pending_payment'})
        self.assertEqual(response.json()['management_status'], 'pending_payment')

    def test_booking_payment_state_is_visible_only_to_admin(self):
        owned = self.booking()
        guest = self.booking(guest=True)
        admin_headers = self.headers(self.admin)
        for booking_id, state in ((owned, 'paid'), (guest, 'pending_payment')):
            response = self.client.patch(f'/admin/classes/{booking_id}/management', headers=admin_headers, json={'management_status': state})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/bookings').status_code, 401)
        student_response = self.client.get('/bookings', headers=self.headers(self.student))
        admin_response = self.client.get('/bookings', headers=admin_headers)
        self.assertEqual(student_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        student_rows = {row['id']: row for row in student_response.json()}
        admin_rows = {row['id']: row for row in admin_response.json()}
        for booking_id, state in ((owned, 'paid'), (guest, 'pending_payment')):
            with self.subTest(booking_id=booking_id):
                self.assertIsNone(student_rows[booking_id].get('management_status'))
                self.assertEqual(student_rows[booking_id]['status'], 'confirmed')
                self.assertIn('user_name', student_rows[booking_id])
                self.assertNotIn('management_status', admin_rows[booking_id])
        admin_classes = {row['id']: row for row in self.client.get('/admin/classes', headers=admin_headers).json()}
        self.assertEqual(admin_classes[guest]['management_status'], 'pending_payment')

    def test_permissions_validation_and_eligible_states(self):
        booking_id = self.booking()
        path = f"/admin/classes/{booking_id}/management"
        body = {"management_status": "paid"}
        self.assertEqual(self.client.patch(path, json=body).status_code, 401)
        self.assertEqual(self.client.patch(path, headers=self.headers(self.student), json=body).status_code, 403)
        for invalid in ("cancelled", "", 1):
            self.assertEqual(self.client.patch(path, headers=self.headers(self.admin), json={"management_status": invalid}).status_code, 422)
        self.assertEqual(self.client.patch("/admin/classes/999999/management", headers=self.headers(self.admin), json=body).status_code, 404)
        for status in ("requested", "declined", "cancelled"):
            other = self.booking(status=status)
            self.assertEqual(self.client.patch(f"/admin/classes/{other}/management", headers=self.headers(self.admin), json=body).status_code, 400)
        future = self.booking(end=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(self.client.patch(f"/admin/classes/{future}/management", headers=self.headers(self.admin), json=body).status_code, 400)

    def test_reset_management_preserves_class_and_allows_schedule_edit(self):
        headers = self.headers(self.admin)
        for state in ('paid', 'pending_payment'):
            with self.subTest(state=state):
                booking_id = self.booking(end=datetime(2026, 1, 5, 11, tzinfo=timezone.utc), guest=True)
                path = f'/admin/classes/{booking_id}/management'
                self.assertEqual(self.client.patch(path, headers=headers, json={'management_status': state}).status_code, 200)
                before = self.client.get('/admin/classes', headers=headers).json()
                before = next(row for row in before if row['id'] == booking_id)
                response = self.client.patch(path, headers=headers, json={'management_status': None})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {**before, 'management_status': None})
                edited = self.client.patch(f'/admin/classes/{booking_id}', headers=headers, json={
                    'starts_at': '2030-01-07T10:00:00Z' if state == 'paid' else '2030-01-07T12:00:00Z', 'duration_slots': 2,
                    'subject': 'Química ESO', 'admin_comment': ''})
                self.assertEqual(edited.status_code, 200, edited.text)

    def test_reset_requires_explicit_field_authorization_and_eligibility(self):
        booking_id = self.booking()
        path = f'/admin/classes/{booking_id}/management'
        body = {'management_status': None}
        self.assertEqual(self.client.patch(path, json=body).status_code, 401)
        self.assertEqual(self.client.patch(path, headers=self.headers(self.student), json=body).status_code, 403)
        self.assertEqual(self.client.patch(path, headers=self.headers(self.admin), json={}).status_code, 422)
        for status in ('requested', 'declined', 'cancelled'):
            other = self.booking(status=status)
            self.assertEqual(self.client.patch(f'/admin/classes/{other}/management', headers=self.headers(self.admin), json=body).status_code, 400)
        future = self.booking(end=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(self.client.patch(f'/admin/classes/{future}/management', headers=self.headers(self.admin), json=body).status_code, 400)

    def test_end_time_boundary_is_inclusive(self):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        at_end = self.booking(end=now)
        before_end = self.booking(end=now + timedelta(microseconds=1))
        with patch("app.main.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            for booking_id, expected in ((at_end, 200), (before_end, 400)):
                result = self.client.patch(f"/admin/classes/{booking_id}/management", headers=self.headers(self.admin), json={"management_status": "paid"})
                self.assertEqual(result.status_code, expected, result.text)

    def test_guest_and_historical_visibility(self):
        guest = self.booking(guest=True)
        headers = self.headers(self.admin)
        rows = self.client.get("/admin/classes", headers=headers).json()
        self.assertIsNone(next(row for row in rows if row["id"] == guest)["management_status"])
        self.assertEqual(self.client.patch(f"/admin/classes/{guest}/management", headers=headers, json={"management_status": "pending_payment"}).status_code, 200)
        student_booking = self.booking()
        self.client.patch(f"/admin/classes/{student_booking}/management", headers=headers, json={"management_status": "paid"})
        self.assertEqual(self.client.patch(f"/admin/classes/{student_booking}/historical", headers=headers, json={"is_historical": True}).json()["management_status"], "paid")
        self.assertNotIn(student_booking, [row["id"] for row in self.client.get("/classes", headers=self.headers(self.student)).json()])
        self.assertIn(student_booking, [row["id"] for row in self.client.get("/admin/classes", headers=headers).json()])

    def test_managed_class_cannot_change_schedule(self):
        headers = self.headers(self.admin)
        for state in ('paid', 'pending_payment'):
            for change in ('start', 'duration'):
                with self.subTest(state=state, change=change):
                    end = datetime(2026, 1, 5, 11, tzinfo=timezone.utc)
                    booking_id = self.booking(end=end)
                    self.assertEqual(self.client.patch(f'/admin/classes/{booking_id}/management', headers=headers, json={'management_status': state}).status_code, 200)
                    start = datetime(2030, 1, 7, 10, tzinfo=timezone.utc) if change == 'start' else end - timedelta(hours=1)
                    response = self.client.patch(f'/admin/classes/{booking_id}', headers=headers, json={
                        'starts_at': start.isoformat(), 'duration_slots': 2 if change == 'start' else 3,
                        'subject': 'Química ESO', 'admin_comment': ''})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn('gestionada', response.json()['detail'])
                    with self.sessions() as db:
                        booking = db.get(models.Booking, booking_id)
                        self.assertEqual(booking.starts_at.replace(tzinfo=timezone.utc), end - timedelta(hours=1))
                        self.assertEqual(booking.management_status, state)

    def test_cancelling_managed_class_clears_payment_state(self):
        headers = self.headers(self.admin)
        for state in ('paid', 'pending_payment'):
            with self.subTest(state=state):
                booking_id = self.booking(end=datetime(2026, 1, 5, 11, tzinfo=timezone.utc))
                self.assertEqual(self.client.patch(f'/admin/classes/{booking_id}/management', headers=headers, json={'management_status': state}).status_code, 200)
                response = self.client.post(f'/admin/classes/{booking_id}/cancel', headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()['management_status'])
                with self.sessions() as db:
                    booking = db.get(models.Booking, booking_id)
                    self.assertEqual((booking.status, booking.management_status), ('cancelled', None))
        from app.admin_reports import enqueue_due_reports
        enqueue_due_reports(self.sessions, datetime(2026, 2, 1, 10, tzinfo=timezone.utc))
        with self.sessions() as db:
            reports = db.query(models.NotificationEvent).filter(models.NotificationEvent.logical_key.like('report:monthly:%')).all()
            self.assertTrue(reports)
            self.assertNotIn('Student', reports[0].body)


class LegacyManagementMigrationTest(unittest.TestCase):
    def test_postgres_existing_column_does_not_issue_ddl(self):
        from app.database import ensure_booking_management_column
        bind = MagicMock()
        bind.dialect.name = 'postgresql'
        inspector = MagicMock()
        inspector.has_table.return_value = True
        inspector.get_columns.return_value = [{'name': 'management_status'}]
        with patch('app.database.inspect', return_value=inspector):
            ensure_booking_management_column(bind)
        bind.begin.assert_not_called()

    def test_existing_bookings_are_preserved_and_migration_is_idempotent(self):
        from app.database import ensure_booking_management_column
        postgres = ephemeral_postgres()
        database = postgres.__enter__()
        self.addCleanup(postgres.__exit__, None, None, None)
        engine = database.engine
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE bookings (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
            connection.execute(text("INSERT INTO bookings (id, status) VALUES (1, :status)"), {"status": "confirmed"})
        ensure_booking_management_column(engine)
        ensure_booking_management_column(engine)
        self.assertIn("management_status", {c["name"] for c in inspect(engine).get_columns("bookings")})
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT status, management_status FROM bookings")).one(), ("confirmed", None))
        engine.dispose()
