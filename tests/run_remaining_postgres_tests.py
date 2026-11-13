"""Run non-fixture tests on isolated PostgreSQL databases on the host."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from sqlalchemy import create_engine
from ephemeral_postgres import ephemeral_postgres
from ephemeral_manifest import host_test_files

ROOT = Path(__file__).resolve().parent.parent
DATABASES = {
    'ISOLATED_POSTGRES_TEST_URL': 'booking_disposable',
    'TEST_REPORT_DATABASE_URL': 'reports_disposable',
    'OAUTH_ISOLATED_POSTGRES_TEST_URL': 'oauth_disposable',
    'MICROSOFT_SCHEMA_TEST_POSTGRES_URL': 'schema_disposable',
    'TEAMS_TEST_POSTGRES_URL': 'teams_disposable',
    'TEAMS_DELETE_TEST_POSTGRES_URL': 'teams_delete_disposable',
    'MICROSOFT_E2E_POSTGRES_URL': 'microsoft_e2e_disposable',
    'INTEGRATION_TEST_POSTGRES_URL': 'integration_disposable',
    'DATABASE_URL': 'app_startup_disposable',
}


def main():
    with tempfile.TemporaryDirectory(prefix='beciencia-rest-pytest-') as temp_dir:
        with ephemeral_postgres() as postgres:
            admin = create_engine(postgres.engine.url, isolation_level='AUTOCOMMIT')
            try:
                with admin.connect() as connection:
                    for database in DATABASES.values():
                        # Names are a fixed internal list, never external input.
                        connection.exec_driver_sql('CREATE DATABASE "' + database + '"')
            finally:
                admin.dispose()
            env = os.environ.copy()
            for key, database in DATABASES.items():
                env[key] = postgres.engine.url.set(database=database).render_as_string(hide_password=False)
            env['PYTHONPATH'] = str(ROOT)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            env['STORAGE_DIR'] = str(Path(temp_dir) / 'storage')
            env['MICROSOFT_SYNC_ENABLED'] = 'false'
            env['SMTP_MODE'] = 'log'
            ignores = ['--ignore=' + str(path) for path in host_test_files()]
            ignores.extend(['--ignore=' + str(ROOT / 'tests' / 'test_env_ignore.py'),
                            '--ignore=' + str(ROOT / 'tests' / 'test_ephemeral_postgres.py')])
            command = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
                       '--rootdir=' + str(ROOT), str(ROOT / 'tests'), *ignores, *sys.argv[1:]]
            return subprocess.run(command, env=env, cwd=temp_dir, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
