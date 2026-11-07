from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import ensure_application_schema
from app import microsoft_auth_models


@pytest.fixture(params=['sqlite', 'postgresql'])
def db(request, tmp_path):
    admin = None
    schema = None
    if request.param == 'sqlite':
        url = f'sqlite:///{tmp_path / "legacy.sqlite"}'
        engine = create_engine(url)
    else:
        url = os.getenv('MICROSOFT_SCHEMA_TEST_POSTGRES_URL')
        if not url:
            pytest.skip('MICROSOFT_SCHEMA_TEST_POSTGRES_URL must be a disposable database')
        if not url.startswith(('postgresql://', 'postgresql+psycopg2://')):
            pytest.fail('PostgreSQL required')
        admin = create_engine(url)
        schema = 'schema_test_' + uuid4().hex
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        yield engine
    finally:
        engine.dispose()
        if admin and schema:
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def legacy(db, credential=False):
    with db.begin() as conn:
        conn.execute(text('CREATE TABLE microsoft_oauth_generation (id INTEGER PRIMARY KEY, generation INTEGER NOT NULL)'))
        conn.execute(text('INSERT INTO microsoft_oauth_generation VALUES (1, 3)'))
        conn.execute(text('CREATE TABLE microsoft_credentials (id INTEGER PRIMARY KEY, account_object_id VARCHAR(36))'))
        if credential:
            conn.execute(text("INSERT INTO microsoft_credentials VALUES (1, 'old-account')"))


def test_legacy_upgrade_is_idempotent(db):
    legacy(db)
    ensure_application_schema(db)
    ensure_application_schema(db)
    columns = {c['name']: c for c in inspect(db).get_columns('microsoft_oauth_generation')}
    assert columns['account_object_id']['nullable']
    assert columns['tenant_id']['nullable']
    with db.connect() as conn:
        assert conn.execute(text('SELECT generation, account_object_id, tenant_id FROM microsoft_oauth_generation')).one() == (3, None, None)


def test_unbound_credential_requires_explicit_intervention(db):
    legacy(db, credential=True)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='explicit intervention'):
            ensure_application_schema(db)


def test_parallel_migrators(db):
    legacy(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ensure_application_schema(db), range(4)))
    assert {'account_object_id', 'tenant_id'} <= {c['name'] for c in inspect(db).get_columns('microsoft_oauth_generation')}


def test_empty_database_creates_oauth_schema(db):
    ensure_application_schema(db)
    assert {'id', 'generation', 'account_object_id', 'tenant_id'} <= {
        column['name'] for column in inspect(db).get_columns('microsoft_oauth_generation')
    }
    with db.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM microsoft_oauth_generation')).scalar_one() == 0


def legacy_leases(db):
    with db.begin() as conn:
        conn.execute(text('CREATE TABLE microsoft_oauth_generation (id INTEGER PRIMARY KEY, generation INTEGER NOT NULL, account_object_id VARCHAR(36), tenant_id VARCHAR(36))'))
        conn.execute(text("INSERT INTO microsoft_oauth_generation VALUES (1, 3, 'account', 'tenant')"))
        conn.execute(text('CREATE TABLE microsoft_credentials (id INTEGER PRIMARY KEY, refresh_token_ciphertext TEXT NOT NULL)'))
        conn.execute(text("INSERT INTO microsoft_credentials VALUES (1, 'synthetic-ciphertext')"))
        conn.execute(text('CREATE TABLE teams_orphan_events (id INTEGER PRIMARY KEY, completed_at TIMESTAMP)'))
        conn.execute(text("INSERT INTO teams_orphan_events (id, completed_at) VALUES (1, '2020-01-01')"))


def test_legacy_leases_and_orphans_upgrade_without_changing_existing_values(db):
    legacy_leases(db)
    ensure_application_schema(db)
    with db.begin() as conn:
        row = conn.execute(text('SELECT refresh_token_ciphertext, refresh_generation, refresh_lease_id, refresh_lease_expires_at FROM microsoft_credentials')).one()
        assert row == ('synthetic-ciphertext', 0, None, None)
        assert conn.execute(text('SELECT count(*) FROM teams_orphan_events WHERE completed_at IS NOT NULL')).scalar_one() == 1
        conn.execute(text("UPDATE microsoft_credentials SET refresh_generation = 7, refresh_lease_id = 'lease'"))
    ensure_application_schema(db)
    with db.connect() as conn:
        assert conn.execute(text('SELECT refresh_generation, refresh_lease_id FROM microsoft_credentials')).one() == (7, 'lease')
        assert conn.execute(text('SELECT count(*) FROM teams_orphan_events WHERE completed_at IS NOT NULL')).scalar_one() == 1
    for table, name in [('microsoft_credentials', 'refresh_generation')]:
        assert not next(c for c in inspect(db).get_columns(table) if c['name'] == name)['nullable']


def test_parallel_legacy_lease_upgrades(db):
    legacy_leases(db)
    barrier = Barrier(4, timeout=30)

    def migrate(_):
        barrier.wait()
        ensure_application_schema(db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(migrate, range(4)))
    with db.connect() as conn:
        assert conn.execute(text('SELECT refresh_generation FROM microsoft_credentials')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM teams_orphan_events WHERE completed_at IS NOT NULL')).scalar_one() == 1


def test_application_schema_creates_and_upgrades_on_one_connection(tmp_path):
    from app import database, microsoft_auth_models, teams_models

    engine = create_engine(f'sqlite:///{tmp_path / "application.sqlite"}', pool_size=1, max_overflow=0)
    try:
        legacy(engine)
        database.ensure_application_schema(engine)
        assert inspect(engine).has_table('microsoft_credentials')
        assert {'account_object_id', 'tenant_id'} <= {
            c['name'] for c in inspect(engine).get_columns('microsoft_oauth_generation')
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize('with_legacy', [False, True])
def test_postgres_concurrent_application_initialization(with_legacy):
    from app import database, models, microsoft_auth_models, teams_models

    url = os.getenv('MICROSOFT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('MICROSOFT_SCHEMA_TEST_POSTGRES_URL must be a disposable database')
    if not url.startswith(('postgresql://', 'postgresql+psycopg2://')):
        pytest.fail('PostgreSQL required')
    admin = create_engine(url)
    schema = 'schema_test_' + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        if with_legacy:
            legacy(engine)
            with engine.begin() as conn:
                conn.execute(text('CREATE TABLE teams_orphan_events (id INTEGER PRIMARY KEY, completed_at TIMESTAMP)'))
                conn.execute(text("INSERT INTO teams_orphan_events (id, completed_at) VALUES (1, '2020-01-01')"))
        barrier = Barrier(4, timeout=30)

        def initialize(_):
            barrier.wait()
            database.ensure_application_schema(engine)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(initialize, range(4)))
        tables = set(inspect(engine).get_table_names())
        assert set(database.Base.metadata.tables) <= tables
        assert {'account_object_id', 'tenant_id'} <= {
            c['name'] for c in inspect(engine).get_columns('microsoft_oauth_generation')
        }
        assert {'refresh_generation', 'refresh_lease_id', 'refresh_lease_expires_at'} <= {
            c['name'] for c in inspect(engine).get_columns('microsoft_credentials')
        }
        assert inspect(engine).has_table('teams_orphan_events') == with_legacy
        if with_legacy:
            with engine.connect() as conn:
                assert conn.execute(text('SELECT count(*) FROM teams_orphan_events WHERE completed_at IS NOT NULL')).scalar_one() == 1
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
