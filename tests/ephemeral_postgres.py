"""Dedicated local PostgreSQL containers for tests; never uses application data.

Docker and the postgres:16-alpine image must already be available on the host.
Containers are labelled, auto-removed, and only removed by their exact ID.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import Session, sessionmaker
from recover_ephemeral_postgres import process_identity, registry_path


@dataclass(frozen=True)
class PostgresTestDatabase:
    engine: Engine
    session_factory: sessionmaker[Session]
    container_id: str


def _docker(*args, env=None):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                env=env, timeout=90, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('PostgreSQL test container operation failed') from None
    return result.stdout.strip()


@contextmanager
def _case_application_startup(engine, factory):
    """Route real application lifespan work to the current case database."""
    main = sys.modules.get('app.main')
    if main is None:
        from startup_postgres_guard import is_isolated_startup_target
        startup_url = os.environ.get('DATABASE_URL', '')
        if startup_url and is_isolated_startup_target(startup_url):
            main = importlib.import_module('app.main')
    if main is None:
        yield
        return
    database_module = sys.modules['app.database']
    database_previous = {name: getattr(database_module, name) for name in ('engine', 'SessionLocal')}
    names = ('ensure_booking_management_column', 'ensure_public_resource_columns')
    previous = {name: getattr(main, name) for name in ('engine', 'SessionLocal', *names)}
    try:
        setattr(main, 'engine', engine)
        setattr(main, 'SessionLocal', factory)
        setattr(database_module, 'engine', engine)
        setattr(database_module, 'SessionLocal', factory)
        for name in names:
            operation = previous[name]
            setattr(main, name, lambda bind=engine, operation=operation: operation(bind))
        yield
    finally:
        for name, value in previous.items():
            setattr(main, name, value)
        for name, value in database_previous.items():
            setattr(database_module, name, value)


@contextmanager
def ephemeral_postgres():
    owner_identity = process_identity(os.getpid())
    if not owner_identity:
        raise RuntimeError('cannot establish test process identity')
    token = uuid.uuid4().hex
    name = 'beciencia-pytest-' + token
    password = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env['POSTGRES_PASSWORD'] = password
    registry = registry_path()
    registry.mkdir(parents=True, exist_ok=True)
    record_path = registry / (token + '.json')
    ownership = {'container_id': None, 'run_token': token,
                 'pid': os.getpid(), 'process_identity': owner_identity}
    with record_path.open('x', encoding='utf-8') as record:
        json.dump(ownership, record)
        record.flush()
        os.fsync(record.fileno())
    # The password is passed by environment, not in Docker arguments or output.
    container_id = _docker('run', '--detach', '--rm', '--pull=never',
                           '--name', name,
                           '--label', 'beciencia.pytest.run=' + token,
                           '--publish', '127.0.0.1::5432',
                           '--tmpfs', '/var/lib/postgresql/data',
                           '--env', 'POSTGRES_PASSWORD',
                           '--env', 'POSTGRES_USER=beciencia_test',
                           '--env', 'POSTGRES_DB=beciencia_test',
                           'postgres:16-alpine', env=env)
    engine = None
    temp_record = registry / (token + '.tmp')
    try:
        with temp_record.open('x', encoding='utf-8') as record:
            json.dump({**ownership, 'container_id': container_id}, record)
            record.flush()
            os.fsync(record.fileno())
        os.replace(temp_record, record_path)
        actual = _docker('inspect', '--format', '{{index .Config.Labels "beciencia.pytest.run"}}', container_id)
        if actual != token or len(container_id) != 64:
            raise RuntimeError('PostgreSQL test container identity mismatch')
        port = _docker('inspect', '--format', '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', container_id)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise RuntimeError('PostgreSQL test container port unavailable')
        url = URL.create('postgresql+psycopg2', username='beciencia_test',
                         password=password, host='127.0.0.1', port=int(port),
                         database='beciencia_test')
        engine = create_engine(url, pool_pre_ping=True)
        deadline = time.monotonic() + 60
        while True:
            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql('SELECT 1')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('PostgreSQL test container not ready') from None
                time.sleep(0.25)
        application_factory = sessionmaker(bind=engine, autoflush=False)
        case_factory = sessionmaker(bind=engine)
        with _case_application_startup(engine, application_factory):
            yield PostgresTestDatabase(engine, case_factory, container_id)
    finally:
        original_error = sys.exc_info()[1]
        try:
            if engine is not None:
                engine.dispose()
            # The ID comes from our successful docker run, not an external URL/name.
            try:
                _docker('stop', '--time', '2', container_id)
            except RuntimeError:
                if _docker('ps', '-a', '--filter', 'id=' + container_id, '--format', '{{.ID}}'):
                    raise
            deadline = time.monotonic() + 10
            while _docker('ps', '-a', '--filter', 'id=' + container_id, '--format', '{{.ID}}'):
                if time.monotonic() >= deadline:
                    raise RuntimeError('PostgreSQL test container cleanup failed')
                time.sleep(0.1)
            temp_record.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
        except Exception as cleanup_error:
            if original_error is not None:
                original_error.add_note('PostgreSQL test container cleanup also failed; ownership record retained')
                raise original_error from cleanup_error
            raise


@pytest.fixture
def postgres_test_db():
    with ephemeral_postgres() as database:
        yield database
