from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event
from test_material_metadata import setup, auth
from app import models


def test_uncategorized_reorder_serializes_real_concurrent_requests(setup):
    client, sessions, engine, ids = setup
    with sessions.begin() as db:
        db.get(models.Material, ids['material']).folder_id = None
        db.add(models.Material(title='Third', kind='pdf', filename='third.pdf', storage_key='third.pdf', content_type='application/pdf', size_bytes=1))
    def order():
        rows = client.get('/admin/materials', headers=auth(ids['admin'])).json()
        return [m['id'] for m in rows if not m.get('folder')]
    original = order()
    assert len(original) == 3
    selected, release = Event(), Event()
    def pause_first(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if 'from materials left outer join material_order' in sql and 'folder_id is null' in sql and not selected.is_set():
            selected.set()
            assert release.wait(10)
    def move():
        return client.post(f"/admin/materials/{original[-1]}/order", json={'direction': 'up'}, headers=auth(ids['admin'])).status_code
    event.listen(engine, 'after_cursor_execute', pause_first)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(move)
            assert selected.wait(10)
            second = executor.submit(move)
            try:
                second.result(timeout=0.5)
            except TimeoutError:
                pass
            release.set()
            statuses = [first.result(timeout=10), second.result(timeout=10)]
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_first)
    assert statuses == [204, 204]
    assert order() == [original[2], original[0], original[1]]
