"""The local URL guard must not accept an arbitrary database role."""
import pytest

from postgres_target_guard import validate_local_test_url


def test_url_guard_rejects_unapproved_role():
    with pytest.raises(ValueError, match='PostgreSQL test URL rejected'):
        validate_local_test_url('postgresql://other:placeholder@db/beciencia_pytest_disposable')


def test_url_guard_accepts_authorized_local_role():
    result = validate_local_test_url('postgresql://beciencia:placeholder@db/beciencia_pytest_disposable')
    assert result.username == 'beciencia'
