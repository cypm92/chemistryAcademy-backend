"""Non-destructive checks; URL validation alone does not attest a server."""
import pytest

from postgres_target_guard import validate_local_test_url


@pytest.mark.parametrize('url', [
    None,
    '',
    'sqlite:///beciencia_pytest_disposable',
    'postgresql://test:secret@production:5432/beciencia_pytest_disposable',
    'postgresql://test:secret@db:5432/postgres',
    'postgresql://test:secret@db:5433/beciencia_pytest_disposable',
    'postgresql://test:secret@db/beciencia_pytest_disposable?host=production',
    'postgresql://test:secret@db/beciencia_pytest_disposable?options=-csearch_path%3Dpublic',
])
def test_rejects_invalid_destination_without_exposing_password(url):
    with pytest.raises(ValueError) as failure:
        validate_local_test_url(url)
    assert 'secret' not in str(failure.value)


def test_accepts_only_the_local_compose_url_shape():
    url = 'postgresql://beciencia:***@db:5432/beciencia_pytest_disposable'
    assert validate_local_test_url(url).database == 'beciencia_pytest_disposable'
