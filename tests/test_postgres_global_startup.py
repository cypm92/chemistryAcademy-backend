"""Verify the app's import-time engine uses the isolated startup PostgreSQL."""
from sqlalchemy import text
from app.database import engine


def test_global_startup_engine_is_postgresql_and_isolated():
    assert engine.dialect.name == 'postgresql'
    with engine.connect() as connection:
        assert connection.execute(text('SELECT current_database()')).scalar_one() == 'app_startup_disposable'
