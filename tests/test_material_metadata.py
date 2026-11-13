from datetime import datetime, timedelta, timezone


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from ephemeral_postgres import postgres_test_db

from app import models
from app.database import Base, get_db, engine as startup_engine
from app.main import app
from app.security import create_token


@pytest.fixture
def setup(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    with sessions() as db:
        admin = models.User(name='Admin', email='a@example.test', password_hash='x', role='admin')
        old_user = models.User(name='Old', email='o@example.test', password_hash='x', role='guest')
        new_user = models.User(name='New', email='n@example.test', password_hash='x', role='guest')
        direct_user = models.User(name='Direct', email='d@example.test', password_hash='x', role='guest')
        folder = models.Folder(name='Folder')
        old, new = models.Tag(name='OLD'), models.Tag(name='New')
        material = models.Material(title='Original', description='Description', folder=folder, kind='pdf', filename='original.pdf', storage_key='unchanged.pdf', content_type='application/pdf', size_bytes=12)
        other = models.Material(title='Other', kind='pdf', filename='other.pdf', storage_key='other.pdf', content_type='application/pdf', size_bytes=10)
        attachment = models.Material(title='Attachment', kind='pdf', filename='class.pdf', storage_key='class.pdf', content_type='application/pdf', size_bytes=5, is_class_attachment=True)
        material.tag_links.append(models.MaterialTag(tag=old))
        other.tag_links.append(models.MaterialTag(tag=new))
        db.add_all([admin, old_user, new_user, direct_user, material, other, attachment])
        db.flush()
        now = datetime.now(timezone.utc)
        for user, tag in [(old_user, old), (new_user, new)]:
            db.add(models.TagAccessGrant(user_id=user.id, tag_id=tag.id, starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        direct = models.AccessGrant(user_id=direct_user.id, material_id=material.id, starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1))
        favorite = models.MaterialFavorite(user_id=admin.id, material_id=material.id)
        db.add_all([direct, favorite]); db.commit()
        ids = {k: v.id for k, v in {'admin': admin, 'old_user': old_user, 'new_user': new_user, 'direct_user': direct_user, 'material': material, 'other': other, 'attachment': attachment, 'old': old, 'new': new, 'direct': direct, 'favorite': favorite, 'folder': folder}.items()}
    def local_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = local_db
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, sessions, engine, ids
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)


def test_material_test_and_global_engines_use_postgresql(setup):
    from app.database import engine as case_engine
    _, _, test_engine, _ = setup
    assert test_engine.dialect.name == 'postgresql'
    assert startup_engine.dialect.name == 'postgresql'
    assert case_engine is test_engine
    assert test_engine.url.port != startup_engine.url.port
    with startup_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT current_database()').scalar_one() == 'beciencia_test'
    with test_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT current_database()').scalar_one() == 'beciencia_test'


def auth(user_id):
    return {'Authorization': f'Bearer {create_token(user_id)}'}


def update(s, data, user='admin', material='material'):
    client, _, _, ids = s
    return client.patch(f'/admin/materials/{ids[material]}', json=data, headers=auth(ids[user]))


def visible(s, user):
    client, _, _, ids = s
    response = client.get('/materials', headers=auth(ids[user]))
    assert response.status_code == 200
    return {item['id'] for item in response.json()}


def test_admin_sharing_includes_direct_and_tag_access_once(setup):
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        now = datetime.now(timezone.utc)
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'],
            starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    response = client.get('/admin/materials', headers=auth(ids['admin']))
    assert response.status_code == 200
    material = next(row for row in response.json() if row['id'] == ids['material'])
    sharing = material['sharing']
    old = [row for row in sharing if row['user']['id'] == ids['old_user']]
    assert len(old) == 1
    assert old[0]['direct'] is not None
    assert [tag['name'] for tag in old[0]['tags']] == ['OLD']
    response = client.get(f"/admin/users/{ids['old_user']}/material-access", headers=auth(ids['admin']))
    assert response.status_code == 200
    rows = [row for row in response.json() if row['material']['id'] == ids['material']]
    assert len(rows) == 1
    assert rows[0]['direct'] is not None
    assert [tag['name'] for tag in rows[0]['tags']] == ['OLD']


def test_admin_access_queries_do_not_grow_per_material(setup):
    client, sessions, engine, ids = setup
    with sessions.begin() as db:
        for i in range(12):
            db.add(models.Material(title=f'Extra {i}', kind='pdf', filename=f'extra-{i}.pdf',
                storage_key=f'extra-{i}.pdf', content_type='application/pdf', size_bytes=1))
    statements = []
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        text = statement.lower()
        if text.lstrip().startswith('select') and ('access_grants' in text or 'tag_access_grants' in text):
            statements.append(text)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        for path in (f"/admin/users/{ids['old_user']}/material-access", '/admin/materials'):
            statements.clear()
            response = client.get(path, headers=auth(ids['admin']))
            assert response.status_code == 200, response.text
            assert len(statements) <= 4, (path, len(statements))
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def test_student_material_access_keeps_direct_grants_ordered_by_expiry(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        db.get(models.AccessGrant, ids['direct']).expires_at = now + timedelta(days=5)
        db.add(models.AccessGrant(user_id=ids['direct_user'], material_id=ids['other'],
            starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    response = client.get(f"/admin/users/{ids['direct_user']}/material-access", headers=auth(ids['admin']))
    assert response.status_code == 200
    assert [row['material']['id'] for row in response.json() if row['direct']] == [ids['other'], ids['material']]


def test_student_material_access_preserves_expired_direct_grants(setup):
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        grant = db.get(models.AccessGrant, ids['direct'])
        grant.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    response = client.get(f"/admin/users/{ids['direct_user']}/material-access", headers=auth(ids['admin']))
    assert response.status_code == 200
    assert any(row['direct'] and row['direct']['id'] == ids['direct'] for row in response.json())


def test_admin_access_sources_exclude_expired_grants_and_deduplicate(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        material = db.get(models.Material, ids['material'])
        material.tag_links.append(models.MaterialTag(tag_id=ids['new']))
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=ids['new'],
            starts_at=now-timedelta(days=2), expires_at=now-timedelta(days=1)))
    assert client.get(f"/admin/users/{ids['old_user']}/material-access", headers=auth(ids['old_user'])).status_code == 403
    assert client.get('/admin/materials', headers=auth(ids['old_user'])).status_code == 403
    response = client.get('/admin/materials', headers=auth(ids['admin']))
    material = next(row for row in response.json() if row['id'] == ids['material'])
    old = [row for row in material['sharing'] if row['user']['id'] == ids['old_user']]
    assert len(old) == 1
    assert [tag['id'] for tag in old[0]['tags']] == [ids['old']]
    guest = client.get('/materials', headers=auth(ids['old_user']))
    assert sum(row['id'] == ids['material'] for row in guest.json()) == 1
    assert all('sharing' not in row and 'grants' not in row for row in guest.json())


def test_multiple_tag_sources_survive_direct_revocation(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        material = db.get(models.Material, ids['material'])
        material.tag_links.append(models.MaterialTag(tag_id=ids['new']))
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=ids['new'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        direct = models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1))
        db.add(direct); db.flush(); direct_id = direct.id
    response = client.delete(f'/admin/grants/{direct_id}', headers=auth(ids['admin']))
    assert response.status_code == 204
    assert ids['material'] in visible(setup, 'old_user')
    rows = client.get(f"/admin/users/{ids['old_user']}/material-access", headers=auth(ids['admin'])).json()
    row = next(item for item in rows if item['material']['id'] == ids['material'])
    assert row['direct'] is None
    assert {tag['id'] for tag in row['tags']} == {ids['old'], ids['new']}
    assert client.delete(f"/admin/tags/{ids['old']}", headers=auth(ids['admin'])).status_code == 204
    assert ids['material'] in visible(setup, 'old_user')


def test_delete_tag_preserves_independent_access_and_material(setup):
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        material = db.get(models.Material, ids['material'])
        material.tag_links.append(models.MaterialTag(tag_id=ids['new']))
        now = datetime.now(timezone.utc)
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'],
            starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    assert client.delete(f"/admin/tags/{ids['old']}", headers=auth(ids['old_user'])).status_code == 403
    response = client.delete(f"/admin/tags/{ids['old']}", headers=auth(ids['admin']))
    assert response.status_code == 204, response.text
    with sessions() as db:
        assert db.get(models.Tag, ids['old']) is None
        assert db.get(models.Material, ids['material']) is not None
        assert {link.tag_id for link in db.get(models.Material, ids['material']).tag_links} == {ids['new']}
        assert db.scalar(select(models.TagAccessGrant).where(models.TagAccessGrant.tag_id == ids['old'])) is None
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['material'] in visible(setup, 'new_user')
    assert ids['material'] in visible(setup, 'direct_user')
    assert client.delete(f"/admin/tags/{ids['old']}", headers=auth(ids['admin'])).status_code == 404


def test_delete_tag_revokes_only_its_access(setup):
    client, sessions, _, ids = setup
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['material'] in visible(setup, 'direct_user')
    response = client.delete(f"/admin/tags/{ids['old']}", headers=auth(ids['admin']))
    assert response.status_code == 204
    assert ids['material'] not in visible(setup, 'old_user')
    assert ids['material'] in visible(setup, 'direct_user')
    assert ids['other'] in visible(setup, 'new_user')
    with sessions() as db:
        assert db.get(models.AccessGrant, ids['direct']) is not None
        assert db.get(models.Tag, ids['new']) is not None


def test_upload_reuses_existing_tag_without_renaming(setup, tmp_path, monkeypatch):
    from app.main import storage
    monkeypatch.setattr(storage, 'root', tmp_path.resolve())
    client, sessions, _, ids = setup
    response = client.post('/admin/materials', headers=auth(ids['admin']),
                           data={'title': 'Upload', 'folder_id': str(ids['folder']), 'tags': 'new'},
                           files={'file': ('upload.pdf', b'%PDF-1.4\nexample', 'application/pdf')})
    assert response.status_code == 201, response.text
    assert response.json()['tags'][0]['id'] == ids['new']
    with sessions() as db:
        assert db.get(models.Tag, ids['new']).name == 'New'
        assert [link.tag_id for link in db.get(models.Material, ids['other']).tag_links] == [ids['new']]
    assert response.json()['id'] in visible(setup, 'new_user')


def test_upload_and_edit_resolve_legacy_unicode_tag_consistently(setup, tmp_path, monkeypatch):
    from app.main import storage
    monkeypatch.setattr(storage, 'root', tmp_path.resolve())
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        legacy = models.Tag(name='ß')
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
        now = datetime.now(timezone.utc)
        db.add(models.TagAccessGrant(user_id=ids['new_user'], tag_id=legacy_id,
                                     starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    edited = update(setup, {'title': 'Revised', 'tags': ['ss']})
    assert edited.status_code == 200, edited.text
    uploaded = client.post('/admin/materials', headers=auth(ids['admin']),
                           data={'title': 'Upload', 'folder_id': str(ids['folder']), 'tags': 'ss'},
                           files={'file': ('upload.pdf', b'%PDF-1.4\nexample', 'application/pdf')})
    assert uploaded.status_code == 201, uploaded.text
    assert edited.json()['tags'][0] == {'id': legacy_id, 'name': 'ß'}
    assert (uploaded.json()['id'] in visible(setup, 'new_user')) == (ids['material'] in visible(setup, 'new_user'))
    assert uploaded.json()['tags'][0]['id'] == edited.json()['tags'][0]['id']


def test_collision_keeps_existing_tag_and_access_when_editing_title(setup):
    _, sessions, _, ids = setup
    with sessions.begin() as db:
        first, second = models.Tag(name='ß'), models.Tag(name='SS')
        db.add_all([first, second]); db.flush()
        first_id = first.id
        material = db.get(models.Material, ids['material'])
        material.tag_links[:] = [models.MaterialTag(tag=first)]
        now = datetime.now(timezone.utc)
        for user, tag in [(ids['old_user'], first), (ids['new_user'], second)]:
            db.add(models.TagAccessGrant(user_id=user, tag_id=tag.id,
                starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    response = update(setup, {'title': 'Solo título', 'tags': ['ß']})
    assert response.status_code == 200, response.text
    assert [tag['id'] for tag in response.json()['tags']] == [first_id]
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['material'] not in visible(setup, 'new_user')


def test_collision_keeps_both_existing_tags_when_unchanged(setup):
    _, sessions, _, ids = setup
    with sessions.begin() as db:
        first, second = models.Tag(name='pau'), models.Tag(name='PAU')
        db.add_all([first, second]); db.flush()
        tag_ids = {first.id, second.id}
        material = db.get(models.Material, ids['material'])
        material.tag_links[:] = [models.MaterialTag(tag=first), models.MaterialTag(tag=second)]
    response = update(setup, {'title': 'Solo título', 'tags': ['pau', 'PAU']})
    assert response.status_code == 200, response.text
    assert {tag['id'] for tag in response.json()['tags']} == tag_ids
    response = update(setup, {'title': 'No guardar', 'tags': ['PAU']})
    assert response.status_code == 409, response.text
    with sessions() as db:
        material = db.get(models.Material, ids['material'])
        assert material.title == 'Solo título'
        assert {link.tag_id for link in material.tag_links} == tag_ids


def test_collision_rejects_new_selection_on_upload_and_edit(setup, tmp_path, monkeypatch):
    from app.main import storage
    monkeypatch.setattr(storage, 'root', tmp_path.resolve())
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        db.add_all([models.Tag(name='ß'), models.Tag(name='SS')])
    edited = update(setup, {'title': 'Changed', 'tags': ['SS']})
    assert edited.status_code == 409, edited.text
    uploaded = client.post('/admin/materials', headers=auth(ids['admin']),
                           data={'title': 'Upload', 'folder_id': str(ids['folder']), 'tags': 'SS'},
                           files={'file': ('upload.pdf', b'%PDF-1.4\nexample', 'application/pdf')})
    assert uploaded.status_code == 409, uploaded.text
    assert 'ambig' in uploaded.json()['detail'].lower()
    assert not list(tmp_path.iterdir())
    with sessions() as db:
        assert db.get(models.Material, ids['material']).title == 'Original'
        assert [link.tag.name for link in db.get(models.Material, ids['material']).tag_links] == ['OLD']


@pytest.mark.parametrize(('requested', 'existing'), [
    ('ESO_1', 'ESO11'), ('PAU%2', 'PAU222'), (r'ESO\1', r'ESO\\1'),
])
def test_upload_does_not_treat_tag_name_as_pattern(setup, tmp_path, monkeypatch, requested, existing):
    from app.main import storage
    monkeypatch.setattr(storage, 'root', tmp_path.resolve())
    client, sessions, _, ids = setup
    with sessions() as db:
        tag = models.Tag(name=existing)
        db.add(tag); db.commit(); existing_id = tag.id
    response = client.post('/admin/materials', headers=auth(ids['admin']),
                           data={'title': 'Upload', 'folder_id': str(ids['folder']), 'tags': requested},
                           files={'file': ('upload.pdf', b'%PDF-1.4\nexample', 'application/pdf')})
    assert response.status_code == 201, response.text
    assert response.json()['tags'][0]['name'] == requested
    assert response.json()['tags'][0]['id'] != existing_id


def test_edit_updates_access_and_preserves_unrelated_data(setup):
    client, sessions, _, ids = setup
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['material'] not in visible(setup, 'new_user')
    response = update(setup, {'title': '  Revised  ', 'tags': [' new ', 'NEW', ' extra, old ', 'EXTRA']})
    assert response.status_code == 200, response.text
    assert response.json()['title'] == 'Revised'
    assert {t['name'] for t in response.json()['tags']} == {'New', 'EXTRA', 'OLD'}
    assert len(response.json()['tags']) == 3
    response = update(setup, {'title': 'Final', 'tags': ['new']})
    assert response.status_code == 200, response.text
    assert ids['material'] not in visible(setup, 'old_user')
    assert ids['material'] in visible(setup, 'new_user')
    assert ids['material'] in visible(setup, 'direct_user')
    with sessions() as db:
        m = db.get(models.Material, ids['material'])
        assert (m.filename, m.storage_key, m.description, m.folder_id, m.size_bytes) == ('original.pdf', 'unchanged.pdf', 'Description', ids['folder'], 12)
        assert db.get(models.Tag, ids['new']).name == 'New'
        assert db.get(models.Tag, ids['old']).name == 'OLD'
        assert db.get(models.AccessGrant, ids['direct']).material_id == m.id
        assert db.get(models.MaterialFavorite, ids['favorite']).material_id == m.id
        assert [link.tag_id for link in db.get(models.Material, ids['other']).tag_links] == [ids['new']]
    response = update(setup, {'title': 'No tags', 'tags': []})
    assert response.status_code == 200
    assert response.json()['tags'] == []
    assert ids['material'] not in visible(setup, 'new_user')
    assert ids['material'] in visible(setup, 'direct_user')


def test_edit_keeps_file_bytes_and_admin_favorite(setup, tmp_path, monkeypatch):
    from app.main import storage
    monkeypatch.setattr(storage, 'root', tmp_path.resolve())
    original = b'%PDF-1.4\nunchanged'
    (tmp_path / 'unchanged.pdf').write_bytes(original)
    response = update(setup, {'title': 'New title', 'tags': []})
    assert response.status_code == 200
    assert response.json()['is_favorite'] is True
    content = setup[0].get(f"/materials/{setup[3]['material']}/content", headers=auth(setup[3]['direct_user']))
    assert content.status_code == 200
    assert content.content == original
    assert (tmp_path / 'unchanged.pdf').read_bytes() == original


def test_rejects_unauthorized_missing_and_attachment(setup):
    client, _, _, ids = setup
    data = {'title': 'X', 'tags': []}
    assert client.patch(f"/admin/materials/{ids['material']}", json=data).status_code == 401
    assert update(setup, data, user='old_user').status_code == 403
    assert client.patch('/admin/materials/999999', json=data, headers=auth(ids['admin'])).status_code == 404
    assert update(setup, data, material='attachment').status_code == 404


@pytest.mark.parametrize('data', [
    {'title': '', 'tags': []}, {'title': '   ', 'tags': []},
    {'title': 'x' * 181, 'tags': []}, {'title': 'Valid', 'tags': ['x' * 81]},
    {'title': 'Valid', 'tags': [1]}, {'title': 'Valid'}, {'tags': []},
])
def test_invalid_input_has_no_effect(setup, data):
    response = update(setup, data)
    assert response.status_code in (400, 422), response.text
    with setup[1]() as db:
        material = db.get(models.Material, setup[3]['material'])
        assert material.title == 'Original'
        assert [link.tag.name for link in material.tag_links] == ['OLD']


def test_database_failure_rolls_back_all_changes(setup):
    _, sessions, engine, ids = setup
    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO MATERIAL_TAGS'):
            raise RuntimeError('injected database failure')
    event.listen(engine, 'before_cursor_execute', fail_insert)
    try:
        response = update(setup, {'title': 'Changed', 'tags': ['BRAND NEW']})
        assert response.status_code == 500
    finally:
        event.remove(engine, 'before_cursor_execute', fail_insert)
    with sessions() as db:
        material = db.get(models.Material, ids['material'])
        assert material.title == 'Original'
        assert [link.tag.name for link in material.tag_links] == ['OLD']
        assert db.scalar(select(models.Tag).where(models.Tag.name == 'BRAND NEW')) is None
