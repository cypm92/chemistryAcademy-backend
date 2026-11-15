from datetime import datetime, timedelta, timezone
from sqlalchemy import event
from app import models
from test_material_metadata import auth, setup


def test_user_summary_counts_distinct_current_files_and_active_tags_in_bulk(setup):
    client, sessions, engine, ids = setup
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        empty = models.Tag(name='VACIA')
        db.add(empty); db.flush()
        db.add(models.TagAccessGrant(user_id=ids['old_user'], tag_id=empty.id, starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['material'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        db.add(models.AccessGrant(user_id=ids['old_user'], material_id=ids['other'], starts_at=now-timedelta(days=2), expires_at=now-timedelta(days=1)))
        db.add(models.TagAccessGrant(user_id=ids['direct_user'], tag_id=empty.id, starts_at=now+timedelta(days=1), expires_at=now+timedelta(days=2)))
        db.add(models.AccessGrant(user_id=ids['new_user'], material_id=ids['attachment'], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=1)))
        db.get(models.User, ids['direct_user']).is_active = False
    statements = []
    def count(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'): statements.append(statement)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        response = client.get('/admin/users/access-summary', headers=auth(ids['admin']))
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert response.status_code == 200, response.text
    rows = {row['user_id']: row for row in response.json()}
    assert rows[ids['old_user']]['file_count'] == 1
    assert {tag['name'] for tag in rows[ids['old_user']]['tags']} == {'OLD', 'VACIA'}
    assert rows[ids['new_user']]['file_count'] == 1
    assert rows[ids['direct_user']]['file_count'] == 0
    assert not rows[ids['direct_user']]['tags']
    assert len(statements) <= 5
    assert client.get('/admin/users/access-summary', headers=auth(ids['old_user'])).status_code == 403
