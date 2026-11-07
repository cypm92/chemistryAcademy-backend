import pytest
from sqlalchemy import create_engine
from app.database import Base, ensure_application_schema
from app import models, microsoft_auth_models, teams_models

@pytest.mark.parametrize('schema,rows,blocked', [
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [], False),
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [(1, '2020-01-01')], False),
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [(1, None)], True),
    ('id INTEGER PRIMARY KEY', [], False),
    ('id INTEGER PRIMARY KEY', [(1,)], True),
])
def test_legacy_table_read_only(tmp_path, schema, rows, blocked):
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.sqlite"}')
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE TABLE teams_orphan_events ({schema})')
        for row in rows:
            conn.exec_driver_sql('INSERT INTO teams_orphan_events VALUES (' + ','.join('?' for _ in row) + ')', row)
    with engine.connect() as conn:
        before = conn.exec_driver_sql('SELECT * FROM teams_orphan_events').all()
        columns = conn.exec_driver_sql('PRAGMA table_info(teams_orphan_events)').all()
    if blocked:
        with pytest.raises(RuntimeError, match='manual review'):
            ensure_application_schema(engine)
    else:
        ensure_application_schema(engine)
        ensure_application_schema(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT * FROM teams_orphan_events').all() == before
        assert conn.exec_driver_sql('PRAGMA table_info(teams_orphan_events)').all() == columns
    assert 'teams_orphan_events' not in Base.metadata.tables
    engine.dispose()
