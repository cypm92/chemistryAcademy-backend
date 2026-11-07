from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token


@pytest.fixture
def legacy_client():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        admin = models.User(name='Admin', email='admin@example.com', password_hash='x', role='admin')
        student = models.User(name='Student', email='prueba@beciencia.local', password_hash='x', role='student')
        db.add_all([admin, student]); db.flush()
        admin_id, student_id = admin.id, student.id
        start = datetime.now(timezone.utc) + timedelta(days=7)
        db.add_all([
            models.Booking(user_id=student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='requested', note='Química'),
            models.Booking(user_id=student_id, starts_at=start + timedelta(days=1), ends_at=start + timedelta(days=1, hours=1), status='confirmed', note='Química'),
        ])
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, {'Authorization': f'Bearer {create_token(admin_id)}'}, student_id
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.mark.parametrize(('path', 'field'), [('/admin/users', 'email'), ('/admin/booking-requests', 'user_email'), ('/admin/classes', 'user_email')])
def test_legacy_reserved_email_in_responses(legacy_client, path, field):
    client, headers, _ = legacy_client
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert any(item[field] == 'prueba@beciencia.local' for item in response.json())


@pytest.mark.parametrize(('method', 'path', 'body'), [
    ('post', '/auth/register', {'name': 'Test', 'email': 'prueba@beciencia.local', 'password': 'password123'}),
    ('post', '/auth/login', {'email': 'prueba@beciencia.local', 'password': 'password123'}),
    ('post', '/admin/users', {'name': 'Test', 'email': 'prueba@beciencia.local', 'password': 'password123'}),
    ('patch', '/admin/users/{id}', {'email': 'prueba@beciencia.local'}),
])
def test_reserved_email_still_rejected_on_input(legacy_client, method, path, body):
    client, headers, student_id = legacy_client
    response = getattr(client, method)(path.format(id=student_id), headers=headers, json=body)
    assert response.status_code == 422, response.text
