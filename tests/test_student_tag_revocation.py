from datetime import datetime, timedelta, timezone

from app import models
from test_material_metadata import auth, setup, visible


def test_remove_one_tag_from_one_student_preserves_other_paths(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        db.add(models.MaterialTag(material_id=ids['other'], tag_id=ids['old']))
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=ids['new'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        db.add(models.TagAccessGrant(user_id=ids['direct_user'], tag_id=ids['old'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    url = f"/admin/users/{ids['old_user']}/tag-grants/{ids['old']}"
    assert client.delete(url, headers=auth(ids['old_user'])).status_code == 403
    assert client.delete(url, headers=auth(ids['admin'])).status_code == 204
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['other'] in visible(setup, 'old_user')
    assert ids['material'] in visible(setup, 'direct_user')
    with sessions() as db:
        assert db.get(models.Tag, ids['old']) is not None
        assert db.query(models.MaterialTag).filter_by(tag_id=ids['old']).count() == 2


def test_schema_upgrade_does_not_create_or_delete_exclusions(setup):
    from sqlalchemy import inspect, text
    from app.database import ensure_application_schema
    _, _, engine, _ = setup
    assert 'material_access_exclusions' not in models.Base.metadata.tables
    assert not inspect(engine).has_table('material_access_exclusions')
    ensure_application_schema(engine)
    assert not inspect(engine).has_table('material_access_exclusions')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE material_access_exclusions (id INTEGER PRIMARY KEY)'))
        connection.execute(text('INSERT INTO material_access_exclusions (id) VALUES (7)'))
    ensure_application_schema(engine)
    assert inspect(engine).has_table('material_access_exclusions')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT id FROM material_access_exclusions')).scalar_one() == 7


def test_removing_direct_grant_preserves_tag_access(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        db.add(models.TagAccessGrant(user_id=ids['direct_user'], tag_id=ids['old'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    assert client.delete(f"/admin/grants/{ids['direct']}", headers=auth(ids['admin'])).status_code == 204
    assert ids['material'] in visible(setup, 'direct_user')


def test_tag_grant_details_and_update_preserve_other_access_paths(setup):
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=ids['new'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1), can_download=True))
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        grant = db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['old']).one()
        grant_id, starts_at = grant.id, grant.starts_at
    url = f"/admin/users/{ids['old_user']}/tag-grants"
    assert client.get(url, headers=auth(ids['old_user'])).status_code == 403
    response = client.get(url, headers=auth(ids['admin']))
    assert response.status_code == 200
    grants = {item['tag']['id']: item for item in response.json()}
    assert grants[ids['old']]['id'] == grant_id
    assert grants[ids['old']]['can_download'] is False
    end = (now + timedelta(days=10)).isoformat()
    update_url = f'{url}/{ids["old"]}'
    payload = {'grant_id': grant_id, 'expires_at': end, 'can_download': True}
    assert client.patch(update_url, headers=auth(ids['old_user']), json=payload).status_code == 403
    assert client.patch(update_url, headers=auth(ids['admin']), json=payload).status_code == 200
    with sessions() as db:
        grant = db.get(models.TagAccessGrant, grant_id)
        assert grant.starts_at == starts_at and grant.can_download is True
        assert grant.expires_at == datetime.fromisoformat(end)
        assert db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['new']).one().can_download is True
        assert db.query(models.AccessGrant).filter_by(user_id=ids['old_user'], material_id=ids['material']).count() == 1
    assert ids['material'] in visible(setup, 'old_user')
    assert ids['other'] in visible(setup, 'old_user')
    assert client.delete(update_url, headers=auth(ids['admin'])).status_code == 204
    assert client.patch(update_url, headers=auth(ids['admin']), json=payload).status_code == 404
    assert ids['material'] in visible(setup, 'old_user')
    assert client.post('/admin/tag-grants', headers=auth(ids['admin']), json={'user_id': ids['old_user'], 'tag_id': ids['old'], 'expires_at': end, 'can_download': False}).status_code == 200
    assert client.patch(update_url, headers=auth(ids['admin']), json=payload).status_code == 409
    with sessions() as db:
        assert db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['old']).one().can_download is False


def test_download_permission_aggregates_active_direct_and_tag_grants(setup, tmp_path, monkeypatch):
    from app.main import storage
    client, sessions, _, ids = setup
    now = datetime.now(timezone.utc)
    content = tmp_path / 'document.pdf'
    content.write_bytes(b'pdf')
    monkeypatch.setattr(storage, 'path', lambda key: content)
    with sessions.begin() as db:
        tag = db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['old']).one()
        tag.expires_at = now + timedelta(days=10)
        tag.can_download = False
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1), can_download=True))
    listing = client.get('/materials', headers=auth(ids['old_user']))
    row = next(row for row in listing.json() if row['id'] == ids['material'])
    assert row['can_download'] is True
    assert client.get(f"/materials/{ids['material']}/content?download=true", headers=auth(ids['old_user'])).status_code == 200
    with sessions.begin() as db:
        db.query(models.AccessGrant).filter_by(user_id=ids['old_user'], material_id=ids['material']).one().can_download = False
    assert client.get(f"/materials/{ids['material']}/content?download=true", headers=auth(ids['old_user'])).status_code == 403
    with sessions.begin() as db:
        db.add(models.MaterialTag(material_id=ids['material'], tag_id=ids['new']))
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=ids['new'], starts_at=now+timedelta(days=1), expires_at=now+timedelta(days=2), can_download=True))
    assert client.get(f"/materials/{ids['material']}/content?download=true", headers=auth(ids['old_user'])).status_code == 403
    with sessions.begin() as db:
        another = db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['new']).one()
        another.starts_at = now-timedelta(days=1)
    listing = client.get('/materials', headers=auth(ids['old_user']))
    row = next(row for row in listing.json() if row['id'] == ids['material'])
    assert row['can_download'] is True
    assert datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00')) == now + timedelta(days=10)
    assert client.get(f"/materials/{ids['material']}/content?download=true", headers=auth(ids['old_user'])).status_code == 200
    with sessions.begin() as db:
        another = db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['new']).one()
        another.expires_at = now-timedelta(seconds=1)
    assert client.get(f"/materials/{ids['material']}/content?download=true", headers=auth(ids['old_user'])).status_code == 403


def test_tag_edit_waits_for_concurrent_revocation(setup):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from sqlalchemy import event
    client, sessions, engine, ids = setup
    with sessions() as db:
        grant_id = db.query(models.TagAccessGrant).filter_by(user_id=ids['old_user'], tag_id=ids['old']).one().id
    url = f"/admin/users/{ids['old_user']}/tag-grants/{ids['old']}"
    payload = {'grant_id': grant_id, 'expires_at': (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(), 'can_download': True}
    deleted, release = Event(), Event()
    def pause_delete(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lower().lstrip().startswith('delete from tag_access_grants'):
            deleted.set()
            assert release.wait(10)
    event.listen(engine, 'after_cursor_execute', pause_delete)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            removal = pool.submit(lambda: client.delete(url, headers=auth(ids['admin'])).status_code)
            assert deleted.wait(10)
            edit = pool.submit(lambda: client.patch(url, headers=auth(ids['admin']), json=payload).status_code)
            try:
                edit.result(timeout=0.5)
                raise AssertionError('La edición no esperó a la revocación')
            except TimeoutError:
                pass
            release.set()
            assert removal.result(timeout=10) == 204
            assert edit.result(timeout=10) == 404
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_delete)
    with sessions() as db:
        assert db.get(models.TagAccessGrant, grant_id) is None


def test_material_listing_batches_grant_and_serialization_queries(setup):
    from sqlalchemy import event
    client, sessions, engine, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        for index in range(5):
            tag = models.Tag(name=f'BATCH-{index}')
            material = models.Material(title=f'Batch {index}', folder_id=ids['folder'], kind='pdf', filename=f'batch-{index}.pdf', storage_key=f'batch-{index}.pdf', content_type='application/pdf', size_bytes=1)
            material.tag_links.append(models.MaterialTag(tag=tag))
            db.add(material)
            db.flush()
            db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=tag.id, starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
    statements = []
    def count(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        response = client.get('/materials', headers=auth(ids['old_user']))
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert response.status_code == 200
    assert len(response.json()) == 6
    assert len(statements) <= 10, f'{len(statements)} SELECTs for six accessible materials'
