import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class PolicyDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        now = datetime(2029, 1, 1, tzinfo=timezone.utc)
        return now if tz else now.replace(tzinfo=None)


class BookingPolicyTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        def local_db():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = local_db
        self.client = TestClient(app)
        with self.sessions() as db:
            users = [models.User(name=n, email=f'{n}@example.com', password_hash='x', role=r) for n, r in [('admin', 'admin'), ('student', 'guest'), ('other', 'guest')]]
            db.add_all(users); db.commit()
            self.admin, self.student, self.other = [u.id for u in users]

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear(); self.engine.dispose()

    def headers(self, user):
        return {'Authorization': f'Bearer {create_token(user)}'}

    def test_weekend_request_rejected_for_registered_and_guest(self):
        now = datetime.now(timezone.utc)
        saturday = (now + timedelta(days=(5 - now.weekday()) % 7 + 7)).replace(hour=12, minute=0, second=0, microsecond=0)
        payload = {'subject': 'Química ESO', 'starts_at': saturday.isoformat()}
        for path, headers, extra in [('/bookings', self.headers(self.student), {'duration_slots': 2}), ('/public/trial-bookings', {}, {'name': 'Guest', 'email': 'guest@example.com'})]:
            with self.subTest(path=path):
                response = self.client.post(path, headers=headers, json={**payload, **extra})
                self.assertEqual(response.status_code, 400, response.text)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.Booking)))), 0)

    def test_confirmed_cancellation_exactly_24_hours_is_rejected(self):
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz else now.replace(tzinfo=None)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student, starts_at=now + timedelta(hours=24), ends_at=now + timedelta(hours=25), status='confirmed')
            db.add(booking); db.commit(); booking_id = booking.id
        with patch('app.main.datetime', FrozenDatetime):
            response = self.client.post(f'/bookings/{booking_id}/cancel', headers=self.headers(self.student))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('profesor', response.json()['detail'].lower())

    def test_friday_intervals_crossing_midnight_are_rejected(self):
        now = datetime(2029, 1, 1, tzinfo=timezone.utc)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz else now.replace(tzinfo=None)
        for path, headers, extra in [('/bookings', self.headers(self.student), {'duration_slots': 2}), ('/public/trial-bookings', {}, {'name': 'Guest', 'email': 'guest@example.com'})]:
            with self.subTest(path=path), patch('app.main.datetime', FrozenDatetime):
                with self.sessions() as db:
                    db.query(models.NotificationEvent).delete(); db.query(models.Booking).delete(); db.commit()
                response = self.client.post(path, headers=headers, json={'subject': 'Química ESO', 'starts_at': '2030-01-04T22:30:00+00:00', **extra})
                self.assertEqual(response.status_code, 400, response.text)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.Booking)))), 0)

    @patch('app.main.datetime', PolicyDatetime)
    def test_madrid_boundaries_offsets_and_summer_time(self):
        # Friday/Saturday and Sunday/Monday around midnight in winter and summer.
        cases = [
            ('2030-01-04T22:30:00+00:00', 201), ('2030-01-04T23:00:00+00:00', 400),
            ('2030-01-06T22:30:00+00:00', 400), ('2030-01-06T23:00:00+00:00', 201),
            ('2030-07-05T21:30:00+00:00', 201), ('2030-07-05T22:00:00+00:00', 400),
            ('2030-07-07T21:30:00+00:00', 400), ('2030-07-07T22:00:00+00:00', 201),
            ('2030-01-05T04:00:00+05:00', 400),
        ]
        for start, expected in cases:
            for registered in (False, True):
                with self.subTest(start=start, registered=registered):
                    with self.sessions() as db:
                        db.query(models.NotificationEvent).delete(); db.query(models.Booking).delete(); db.commit()
                    payload = {'subject': 'Química ESO', 'starts_at': start}
                    if registered:
                        response = self.client.post('/bookings', headers=self.headers(self.student), json={**payload, 'duration_slots': 1})
                    else:
                        response = self.client.post('/public/trial-bookings', json={**payload, 'name': 'Guest', 'email': 'guest@example.com'})
                    guest_crosses_midnight = not registered and start in {'2030-01-04T22:30:00+00:00', '2030-07-05T21:30:00+00:00'}
                    self.assertEqual(response.status_code, 400 if guest_crosses_midnight else expected, response.text)

    @patch('app.main.datetime', PolicyDatetime)
    def test_guest_may_end_exactly_at_saturday_midnight(self):
        for start in ('2030-01-04T22:00:00+00:00', '2030-07-05T21:00:00+00:00'):
            with self.subTest(start=start):
                with self.sessions() as db:
                    db.query(models.NotificationEvent).delete(); db.query(models.Booking).delete(); db.commit()
                response = self.client.post('/public/trial-bookings', json={'subject': 'Química ESO', 'starts_at': start, 'name': 'Guest', 'email': 'guest@example.com'})
                self.assertEqual(response.status_code, 201, response.text)

    @patch('app.main.datetime', PolicyDatetime)
    def test_admin_can_create_and_edit_weekend_classes(self):
        start = '2030-01-05T12:00:00+00:00'
        body = {'user_id': self.student, 'subject': 'Química ESO', 'starts_at': start, 'duration_slots': 2}
        self.assertEqual(self.client.post('/admin/classes', headers=self.headers(self.student), json=body).status_code, 403)
        response = self.client.post('/admin/classes', headers=self.headers(self.admin), json=body)
        self.assertEqual(response.status_code, 201, response.text)
        booking_id = response.json()['id']
        body['starts_at'] = '2030-01-06T12:00:00+00:00'
        response = self.client.patch(f'/admin/classes/{booking_id}', headers=self.headers(self.admin), json=body)
        self.assertEqual(response.status_code, 200, response.text)

    @patch('app.main.datetime', PolicyDatetime)
    def test_reschedule_offset_persists_utc_and_delivers_notice(self):
        from app.notifications import deliver_pending
        with self.sessions() as db:
            start = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
            booking = models.Booking(user_id=self.student, starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', note='Química ESO')
            db.add(booking); db.commit(); booking_id = booking.id
        response = self.client.patch(f'/admin/classes/{booking_id}', headers=self.headers(self.admin), json={
            'starts_at': '2030-01-02T12:00:00+02:00', 'duration_slots': 2, 'subject': 'Química ESO'})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(models.Booking, booking_id).starts_at.replace(tzinfo=timezone.utc), datetime(2030, 1, 2, 10, tzinfo=timezone.utc))
        with patch('app.notifications._send', return_value=True) as send:
            deliver_pending(self.sessions)
        self.assertGreater(send.call_count, 0)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
            self.assertTrue(all(e.status == 'sent' for e in events))
            self.assertTrue(all('+00:00' in e.logical_key for e in events))

    def test_admin_cancellation_rejects_repeated_and_incompatible_states(self):
        for state in ('confirmed', 'requested', 'cancelled', 'declined'):
            with self.subTest(state=state), self.sessions() as db:
                start = datetime(2030, 1, 1, tzinfo=timezone.utc)
                booking = models.Booking(user_id=self.student, starts_at=start, ends_at=start + timedelta(hours=1), status=state)
                db.add(booking); db.commit(); booking_id = booking.id
                url = f'/admin/classes/{booking_id}/cancel'
                response = self.client.post(url, headers=self.headers(self.admin))
                self.assertEqual(response.status_code, 200 if state in ('confirmed', 'requested') else 400, response.text)
                if response.status_code == 200:
                    count = len(list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like(f'booking:{booking_id}:Clase cancelada:%')))))
                    self.assertGreater(count, 0)
                    self.assertEqual(self.client.post(url, headers=self.headers(self.admin)).status_code, 400)
                    self.assertEqual(len(list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like(f'booking:{booking_id}:Clase cancelada:%'))))), count)

    def test_cancellation_status_cutoffs_and_authorization(self):
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz else now.replace(tzinfo=None)
        for state, delta, expected in [('requested', -1, 200), ('requested', 0, 200), ('confirmed', 86401, 200), ('confirmed', 86399, 400), ('confirmed', -1, 400), ('declined', 90000, 400), ('cancelled', 90000, 400)]:
            with self.subTest(state=state, delta=delta):
                start = now + timedelta(seconds=delta)
                with self.sessions() as db:
                    b = models.Booking(user_id=self.student, starts_at=start, ends_at=start + timedelta(hours=1), status=state)
                    db.add(b); db.commit(); booking_id = b.id
                url = f'/bookings/{booking_id}/cancel'
                with patch('app.main.datetime', FrozenDatetime):
                    self.assertEqual(self.client.post(url).status_code, 401)
                    self.assertEqual(self.client.post(url, headers=self.headers(self.other)).status_code, 404)
                    response = self.client.post(url, headers=self.headers(self.student))
                self.assertEqual(response.status_code, expected, response.text)
                if state == 'confirmed' and expected == 400:
                    self.assertIn('profesor', response.json()['detail'].lower())
                with self.sessions() as db:
                    self.assertEqual(db.get(models.Booking, booking_id).status, 'cancelled' if expected == 200 else state)
