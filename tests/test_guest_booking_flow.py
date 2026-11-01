import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class GuestBookingFlowTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        def get_test_db():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = get_test_db
        self.client = TestClient(app)
        with self.sessions() as db:
            admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
            student = models.User(name='Student', email='student@example.com', password_hash='x', role='guest')
            db.add_all([admin, student]); db.commit()
            self.admin, self.student = admin.id, student.id
        self.start = (datetime.now(timezone.utc) + timedelta(days=10)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.start += timedelta(days=(-self.start.weekday()) % 7)

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear(); self.engine.dispose()

    def headers(self, user):
        return {'Authorization': f'Bearer {create_token(user)}'}

    def events(self, booking_id):
        with self.sessions() as db:
            return list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like(f'booking:{booking_id}:%'))))

    def test_registered_and_guest_decisions_and_authorization(self):
        for index, registered in enumerate((False, True)):
            for j, decision in enumerate(('confirmed', 'declined')):
                start = self.start + timedelta(days=index * 7 + j)
                if registered:
                    response = self.client.post('/bookings', headers=self.headers(self.student), json={'subject': 'Química ESO', 'starts_at': start.isoformat(), 'duration_slots': 2})
                    email = 'student@example.com'
                else:
                    response = self.client.post('/public/trial-bookings', json={'name': 'Invitada', 'email': 'invitada@example.com', 'subject': 'Química ESO', 'starts_at': start.isoformat()})
                    email = 'invitada@example.com'
                self.assertEqual(response.status_code, 201, response.text)
                booking_id = response.json()['id']
                events = self.events(booking_id)
                initial = next(e for e in events if e.recipient == email)
                self.assertIn('pendiente', initial.body.lower())
                self.assertNotEqual(initial.subject, 'Clase confirmada')
                path = f'/admin/bookings/{booking_id}'
                self.assertEqual(self.client.patch(path, json={'status': decision}).status_code, 401)
                self.assertEqual(self.client.patch(path, headers=self.headers(self.student), json={'status': decision}).status_code, 403)
                result = self.client.patch(path, headers=self.headers(self.admin), json={'status': decision})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['status'], decision)
                final = [e for e in self.events(booking_id) if e.recipient == email]
                self.assertEqual(len(final), 2)
                self.assertEqual(final[-1].subject, 'Clase confirmada' if decision == 'confirmed' else 'Solicitud de clase rechazada')
                self.assertEqual(self.client.patch(path, headers=self.headers(self.admin), json={'status': decision}).status_code, 404)

    def test_guest_class_management_and_blocks(self):
        response = self.client.post('/public/trial-bookings', json={'name': 'Invitada', 'email': 'invitada@example.com', 'subject': 'Química ESO', 'starts_at': self.start.isoformat()})
        booking_id = response.json()['id']
        admin = self.headers(self.admin)
        with self.sessions() as db:
            db.add(models.Booking(starts_at=self.start + timedelta(days=1), ends_at=self.start + timedelta(days=1, hours=1), status='blocked'))
            db.commit()
        self.assertEqual(len(self.client.get('/admin/classes', headers=admin).json()), 1)
        edit = self.client.patch(f'/admin/classes/{booking_id}', headers=admin, json={'starts_at': (self.start + timedelta(hours=2)).isoformat(), 'duration_slots': 2, 'subject': 'Química ESO', 'admin_comment': 'Nota'})
        self.assertEqual(edit.status_code, 200, edit.text)
        historical = self.client.patch(f'/admin/classes/{booking_id}/historical', headers=admin, json={'is_historical': True})
        self.assertEqual(historical.status_code, 200, historical.text)
        self.assertEqual(self.client.post(f'/admin/classes/{booking_id}/cancel', headers=admin).status_code, 200)
        self.assertEqual(self.client.delete(f'/admin/classes/{booking_id}', headers=admin).status_code, 204)

    def test_guest_request_visible_and_decidable_with_private_pending_notice(self):
        response = self.client.post('/public/trial-bookings', json={'name': 'Invitada', 'email': 'invitada@example.com', 'subject': 'Química ESO', 'starts_at': self.start.isoformat()})
        self.assertEqual(response.status_code, 201, response.text)
        booking_id = response.json()['id']
        self.assertEqual(response.json()['status'], 'requested')
        events = self.events(booking_id)
        student_event = next(e for e in events if e.recipient == 'invitada@example.com')
        self.assertIn('pendiente', student_event.body.lower())
        self.assertNotEqual(student_event.subject, 'Clase confirmada')
        rows = self.client.get('/admin/classes', headers=self.headers(self.admin)).json()
        item = next(row for row in rows if row['id'] == booking_id)
        self.assertEqual((item['user_id'], item['user_name'], item['user_email']), (None, 'Invitada', 'invitada@example.com'))
        pending = self.client.get('/admin/booking-requests', headers=self.headers(self.admin)).json()
        self.assertEqual(next(row for row in pending if row['id'] == booking_id)['user_email'], 'invitada@example.com')
        self.assertEqual(self.client.get('/admin/classes').status_code, 401)
        self.assertEqual(self.client.get('/admin/classes', headers=self.headers(self.student)).status_code, 403)
        self.assertNotIn('invitada@example.com', self.client.get('/bookings', headers=self.headers(self.student)).text)
        self.assertEqual(self.client.get('/classes', headers=self.headers(self.student)).json(), [])
        response = self.client.patch(f'/admin/bookings/{booking_id}', headers=self.headers(self.admin), json={'status': 'confirmed'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'confirmed')
        events = self.events(booking_id)
        self.assertEqual([e.subject for e in events if e.recipient == 'invitada@example.com'].count('Clase confirmada'), 1)
