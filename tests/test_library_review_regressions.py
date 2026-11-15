from sqlalchemy import event
from test_material_metadata import setup, auth
from app import models


def test_deleting_material_removes_its_order(setup):
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        db.add(models.MaterialOrder(material_id=ids['material'], position=5))

    assert client.delete(f"/admin/materials/{ids['material']}", headers=auth(ids['admin'])).status_code == 204
    with sessions() as db:
        assert db.get(models.MaterialOrder, ids['material']) is None



def test_deletion_waits_until_folder_reorder_commits(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client, sessions, engine, ids = setup
    with sessions.begin() as db:
        db.get(models.Material, ids['other']).folder_id = ids['folder']
    rows = client.get('/admin/materials', headers=auth(ids['admin'])).json()
    original = [row['id'] for row in rows if row.get('folder') and row['folder']['id'] == ids['folder']]
    assert len(original) == 2
    selected, release = Event(), Event()
    def pause_order(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if 'from materials left outer join material_order' in sql and 'folder_id =' in sql and not selected.is_set():
            selected.set()
            assert release.wait(10)
    event.listen(engine, 'after_cursor_execute', pause_order)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reorder = pool.submit(lambda: client.post(f'/admin/materials/{original[1]}/order', json={'direction': 'up'}, headers=auth(ids['admin'])).status_code)
            assert selected.wait(10)
            deletion = pool.submit(lambda: client.delete(f'/admin/materials/{original[0]}', headers=auth(ids['admin'])).status_code)
            try:
                deletion.result(timeout=0.5)
            except TimeoutError:
                pass
            else:
                release.set()
                raise AssertionError('La eliminación no esperó a la reordenación')
            release.set()
            assert reorder.result(timeout=10) == 204
            assert deletion.result(timeout=10) == 204
        with sessions() as db:
            assert db.get(models.Material, original[0]) is None
            assert db.get(models.MaterialOrder, original[0]) is None
            assert db.get(models.Material, original[1]) is not None
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_order)


def test_reorder_returns_not_found_when_target_deleted_while_waiting(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client, sessions, engine, ids = setup
    with sessions.begin() as db:
        db.get(models.Material, ids['other']).folder_id = ids['folder']
    deleted, release = Event(), Event()
    def pause_delete(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lower().lstrip().startswith('delete from materials'):
            deleted.set()
            assert release.wait(10)
    event.listen(engine, 'after_cursor_execute', pause_delete)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            deletion = pool.submit(lambda: client.delete(f"/admin/materials/{ids['material']}", headers=auth(ids['admin'])).status_code)
            assert deleted.wait(10)
            reorder = pool.submit(lambda: client.post(f"/admin/materials/{ids['material']}/order", json={'direction':'up'}, headers=auth(ids['admin'])).status_code)
            try:
                reorder.result(timeout=0.5)
            except TimeoutError:
                pass
            release.set()
            assert deletion.result(timeout=10) == 204
            assert reorder.result(timeout=10) == 404
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_delete)


def test_two_reorders_in_same_real_folder_are_serialized(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client, sessions, engine, ids = setup
    with sessions.begin() as db:
        db.get(models.Material, ids['other']).folder_id = ids['folder']
        db.add(models.Material(title='Third', folder_id=ids['folder'], kind='pdf', filename='third.pdf',
                               storage_key='third-folder.pdf', content_type='application/pdf', size_bytes=1))
    def order():
        rows = client.get('/admin/materials', headers=auth(ids['admin'])).json()
        return [row['id'] for row in rows if row.get('folder') and row['folder']['id'] == ids['folder']]
    original = order()
    assert len(original) == 3
    selected, release = Event(), Event()
    def pause_first(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if 'from materials left outer join material_order' in sql and 'folder_id =' in sql and not selected.is_set():
            selected.set()
            assert release.wait(10)
    def move():
        return client.post(f"/admin/materials/{original[-1]}/order", json={'direction':'up'}, headers=auth(ids['admin'])).status_code
    event.listen(engine, 'after_cursor_execute', pause_first)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(move)
            assert selected.wait(10)
            second = pool.submit(move)
            try:
                second.result(timeout=0.5)
            except TimeoutError:
                pass
            release.set()
            assert [first.result(timeout=10), second.result(timeout=10)] == [204, 204]
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_first)
    assert order() == [original[2], original[0], original[1]]
