"""Reject application test collection without an explicit PostgreSQL target."""
import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from startup_postgres_guard import is_isolated_startup_target


_NO_APPLICATION = {
    'test_env_ignore.py',
    'test_ephemeral_manifest.py',
    'test_ephemeral_postgres.py',
    'test_ephemeral_recovery.py',
    'test_postgres_target_guard.py',
    'test_postgres_authorized_role.py',
    'test_pytest_requires_postgres.py',
}


def pytest_sessionstart(session):
    args = session.config.args
    selected = [str(arg).split('::', 1)[0] for arg in args]
    exempt_paths = {Path(__file__).resolve().parent / name for name in _NO_APPLICATION}
    if selected and all(Path(arg).resolve() in exempt_paths for arg in selected):
        return
    try:
        url = make_url(os.environ.get('DATABASE_URL', ''))
        if url.get_backend_name() == 'postgresql':
            if is_isolated_startup_target(os.environ['DATABASE_URL']):
                return
            raise pytest.UsageError('isolated PostgreSQL server identity could not be verified')
    except (ArgumentError, ValueError, TypeError):
        pass
    raise pytest.UsageError('PostgreSQL required for application tests; configure an isolated DATABASE_URL')
