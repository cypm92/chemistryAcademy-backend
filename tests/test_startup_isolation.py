"""Application lifespan must not touch the shared startup database per case."""
from fastapi.testclient import TestClient
import pytest
import subprocess
import sys
from sqlalchemy import event
from ephemeral_postgres import ephemeral_postgres, postgres_test_db
from app import database, main
from app.database import Base, engine as startup_engine


def test_lifespan_uses_only_current_case_database(postgres_test_db):
    Base.metadata.create_all(postgres_test_db.engine)
    statements = []
    case_statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def record_case(conn, cursor, statement, parameters, context, executemany):
        case_statements.append(statement)
    event.listen(startup_engine, 'before_cursor_execute', record)
    event.listen(postgres_test_db.engine, 'before_cursor_execute', record_case)
    try:
        with TestClient(main.app):
            pass
    finally:
        event.remove(startup_engine, 'before_cursor_execute', record)
        event.remove(postgres_test_db.engine, 'before_cursor_execute', record_case)
    assert not statements, 'application lifespan touched shared startup database'
    assert any('UPDATE public_resources' in statement for statement in case_statements)


def test_nested_case_restores_outer_application_engine(postgres_test_db):
    assert main.engine is postgres_test_db.engine
    with ephemeral_postgres() as inner:
        assert main.engine is inner.engine
        Base.metadata.create_all(inner.engine)
        with TestClient(main.app):
            pass
    assert main.engine is postgres_test_db.engine


def test_exception_restores_original_application_and_database_bindings():
    before = (main.engine, main.SessionLocal, database.engine, database.SessionLocal)
    with pytest.raises(ValueError, match='case failed'):
        with ephemeral_postgres() as case:
            assert main.engine is case.engine and database.engine is case.engine
            assert main.SessionLocal.kw['autoflush'] is False
            assert database.SessionLocal.kw['autoflush'] is False
            assert case.session_factory.kw['autoflush'] is True
            raise ValueError('case failed')
    assert (main.engine, main.SessionLocal, database.engine, database.SessionLocal) == before


def test_late_main_import_still_isolates_application_lifespan():
    code = '''
from sqlalchemy import event
from app.database import Base, engine as startup
from ephemeral_postgres import ephemeral_postgres
seen = []
def record(*args): seen.append(args[2])
event.listen(startup, 'before_cursor_execute', record)
with ephemeral_postgres() as case:
    from app import main
    from fastapi.testclient import TestClient
    Base.metadata.create_all(case.engine)
    with TestClient(main.app): pass
assert not seen, 'lifespan touched shared startup engine after late import'
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_case_entered_before_any_app_import_still_binds_engine():
    code = '''
from ephemeral_postgres import ephemeral_postgres
with ephemeral_postgres() as case:
    from app import main
    assert main.engine is case.engine
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
