import pytest
from sqlalchemy import inspect
from ephemeral_postgres import postgres_test_db
from app.database import Base, ensure_application_schema
from app import models, microsoft_auth_models, teams_models


def column_snapshot(conn):
    return [dict(column, type=str(column['type']))
            for column in inspect(conn).get_columns('teams_orphan_events')]

@pytest.mark.parametrize('schema,rows,blocked', [
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [], False),
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [(1, '2020-01-01')], False),
    ('id INTEGER PRIMARY KEY, completed_at TIMESTAMP', [(1, None)], True),
    ('id INTEGER PRIMARY KEY', [], False),
    ('id INTEGER PRIMARY KEY', [(1,)], True),
])
def test_legacy_table_read_only(postgres_test_db, schema, rows, blocked):
    engine = postgres_test_db.engine
    assert engine.dialect.name == 'postgresql'
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE TABLE teams_orphan_events ({schema})')
        for row in rows:
            conn.exec_driver_sql('INSERT INTO teams_orphan_events VALUES (' + ','.join('%s' for _ in row) + ')', row)
    with engine.connect() as conn:
        before = conn.exec_driver_sql('SELECT * FROM teams_orphan_events').all()
        columns = column_snapshot(conn)
    if blocked:
        with pytest.raises(RuntimeError, match='manual review'):
            ensure_application_schema(engine)
    else:
        ensure_application_schema(engine)
        ensure_application_schema(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT * FROM teams_orphan_events').all() == before
        assert column_snapshot(conn) == columns
    assert 'teams_orphan_events' not in Base.metadata.tables
    engine.dispose()
