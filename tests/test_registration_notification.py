import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app


class RegistrationNotificationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

        def local_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = local_db
        self.client = TestClient(app, raise_server_exceptions=False)
        self.payload = {'name': 'Lucía Ejemplo', 'email': 'LUCIA@example.com', 'password': 'test-password-only'}

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_success_creates_one_admin_event_without_credentials(self):
        response = self.client.post('/auth/register', json=self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        with self.sessions() as db:
            user = db.scalar(select(models.User).where(models.User.email == 'lucia@example.com'))
            events = list(db.scalars(select(models.NotificationEvent)))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].recipient, 'clases@beciencia.es')
            self.assertEqual(events[0].logical_key, f'registration:{user.id}:admin:clases@beciencia.es')
            self.assertEqual(events[0].subject, 'Nuevo alumno registrado')
            self.assertIn('Lucía Ejemplo', events[0].body)
            self.assertIn('lucia@example.com', events[0].body)
            self.assertNotIn(self.payload['password'], events[0].body)
            self.assertNotIn(user.password_hash, events[0].body)
        duplicate = self.client.post('/auth/register', json=self.payload)
        self.assertEqual(duplicate.status_code, 400)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.NotificationEvent)))), 1)

    def test_notification_failure_rolls_back_user_and_event(self):
        with patch('app.main.enqueue_registration', side_effect=RuntimeError('simulated')):
            response = self.client.post('/auth/register', json=self.payload)
        self.assertEqual(response.status_code, 500)
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(models.User).where(models.User.email == 'lucia@example.com')))
            self.assertEqual(list(db.scalars(select(models.NotificationEvent))), [])


if __name__ == '__main__':
    unittest.main()
