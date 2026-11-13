from ephemeral_manifest import host_test_files, shard_host_files
from pathlib import Path
import pytest


def test_host_manifest_contains_migrated_tests_and_not_runner_self_tests():
    names = {path.name for path in host_test_files()}
    assert 'test_material_metadata.py' in names
    assert 'test_authorization.py' in names
    assert 'test_microsoft_auth.py' in names
    assert 'test_teams_missing_event.py' in names
    assert 'test_ephemeral_postgres.py' not in names
    assert 'test_postgres_target_guard.py' not in names


def test_host_shards_partition_the_complete_manifest_once():
    files = host_test_files()
    shards = [shard_host_files(files, index, 2) for index in range(2)]
    combined = [path for shard in shards for path in shard]
    assert all(shards)
    assert len(combined) == len(set(combined)) == len(files)
    assert set(combined) == set(files)


@pytest.mark.parametrize(('index', 'count'), [(0, 0), (-1, 2), (2, 2)])
def test_host_shards_reject_invalid_indexes(index, count):
    with pytest.raises(ValueError, match='invalid shard'):
        shard_host_files([Path('test_example.py')], index, count)


def test_host_tests_do_not_skip_when_postgres_url_is_missing():
    root = Path(__file__).resolve().parent
    for name in ('test_microsoft_end_to_end_postgres.py', 'test_teams_unknown_delete_safety.py', 'test_teams_postgres.py'):
        assert 'pytest.skip(' not in (root / name).read_text(encoding='utf-8')


def test_race_fixtures_do_not_accept_external_postgres_urls():
    root = Path(__file__).resolve().parent
    names = ('test_admin_reports_postgres.py', 'test_postgres_booking_race.py',
             'test_postgres_real_booking_races.py', 'test_postgres_reminder_race.py',
             'test_teams_edit_concurrency.py')
    for name in names:
        source = (root / name).read_text(encoding='utf-8')
        assert 'TEST_REPORT_DATABASE_URL' not in source
        assert 'ISOLATED_POSTGRES_TEST_URL' not in source
        assert 'TEAMS_TEST_POSTGRES_URL' not in source
