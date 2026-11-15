from app import models
from test_material_metadata import auth, setup


def test_user_roles_cannot_be_changed_and_edits_remain_atomic(setup):
    client, sessions, _, ids = setup
    headers = auth(ids['admin'])
    with sessions.begin() as db:
        other = models.User(name='Other Admin', email='admin2@example.test', password_hash='x', role='admin')
        db.add(other)
        db.flush()
        other_id = other.id
    for user_id, requested, expected in [(ids['old_user'], 'admin', 'guest'), (other_id, 'guest', 'admin')]:
        response = client.patch(f'/admin/users/{user_id}', json={'role': requested, 'name': 'Changed'}, headers=headers)
        assert response.status_code == 400, response.text
        with sessions() as db:
            user = db.get(models.User, user_id)
            assert user.role == expected
            assert user.name != 'Changed'
        response = client.patch(f'/admin/users/{user_id}', json={'role': expected, 'name': 'Updated'}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['role'] == expected
        assert response.json()['name'] == 'Updated'


def test_admin_can_create_admin_and_student_but_student_cannot(setup):
    client, _, _, ids = setup
    for role in ('admin', 'guest'):
        payload = {'name': 'Test User', 'email': f'{role}@example.com', 'password': 'password123', 'role': role}
        assert client.post('/admin/users', json=payload, headers=auth(ids['old_user'])).status_code == 403
        response = client.post('/admin/users', json=payload, headers=auth(ids['admin']))
        assert response.status_code == 201, response.text
        assert response.json()['role'] == role
