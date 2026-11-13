"""Check startup destination attestation against a real isolated container."""
import json
import startup_postgres_guard
from ephemeral_postgres import ephemeral_postgres, postgres_test_db
from recover_ephemeral_postgres import registry_path
from startup_postgres_guard import is_isolated_startup_target


def adjacent_port(port):
    return port - 1 if port == 65535 else port + 1


def test_endpoint_check_survives_adjacent_registered_container(postgres_test_db, tmp_path, monkeypatch):
    registry = registry_path()
    records = [json.loads(path.read_text(encoding='utf-8')) for path in registry.glob('*.json')]
    own = next(record for record in records if record['container_id'] == postgres_test_db.container_id)
    second = {**own, 'container_id': 'a' * 64, 'run_token': 'b' * 32}
    (tmp_path / 'own.json').write_text(json.dumps(own), encoding='utf-8')
    (tmp_path / 'second.json').write_text(json.dumps(second), encoding='utf-8')
    original_format = startup_postgres_guard._docker_format
    def inspect(container, template):
        if container != second['container_id']:
            return original_format(container, template)
        if 'Config.Labels' in template:
            return second['run_token']
        if 'State.Running' in template:
            return 'true true'
        if 'Tmpfs' in template:
            return json.dumps({'/var/lib/postgresql/data': ''})
        return json.dumps({'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(adjacent_port(postgres_test_db.engine.url.port))}]})
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    monkeypatch.setattr(startup_postgres_guard, '_docker_format', inspect)
    adjacent = postgres_test_db.engine.url.set(port=adjacent_port(postgres_test_db.engine.url.port))
    assert is_isolated_startup_target(adjacent.render_as_string(hide_password=False))
    isolated = tmp_path / 'isolated'
    isolated.mkdir()
    test_startup_guard_checks_container_endpoint_and_disallows_url_overrides(postgres_test_db, isolated, monkeypatch)


def test_endpoint_check_with_two_real_live_containers(postgres_test_db, tmp_path, monkeypatch):
    with ephemeral_postgres() as second:
        first_url = postgres_test_db.engine.url.render_as_string(hide_password=False)
        second_url = second.engine.url.render_as_string(hide_password=False)
        assert postgres_test_db.container_id != second.container_id
        assert is_isolated_startup_target(first_url)
        assert is_isolated_startup_target(second_url)
        with postgres_test_db.engine.connect() as first, second.engine.connect() as other:
            assert first.exec_driver_sql('select 1').scalar_one() == 1
            assert other.exec_driver_sql('select 2').scalar_one() == 2
        test_startup_guard_checks_container_endpoint_and_disallows_url_overrides(postgres_test_db, tmp_path, monkeypatch)


def test_startup_guard_checks_container_endpoint_and_disallows_url_overrides(postgres_test_db, tmp_path, monkeypatch):
    records = [json.loads(path.read_text(encoding='utf-8')) for path in registry_path().glob('*.json')]
    own = next(record for record in records if record['container_id'] == postgres_test_db.container_id)
    (tmp_path / 'own.json').write_text(json.dumps(own), encoding='utf-8')
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    url = postgres_test_db.engine.url
    assert is_isolated_startup_target(url.render_as_string(hide_password=False))
    assert not is_isolated_startup_target(url.set(port=adjacent_port(url.port)).render_as_string(hide_password=False))
    assert not is_isolated_startup_target(url.set(host='198.51.100.1').render_as_string(hide_password=False))
    assert not is_isolated_startup_target(url.set(username='other').render_as_string(hide_password=False))
    assert not is_isolated_startup_target(url.set(query={'host': '198.51.100.1'}).render_as_string(hide_password=False))


def test_startup_guard_rejects_wrong_label_and_dead_owner(postgres_test_db, tmp_path, monkeypatch):
    registry = registry_path()
    records = [json.loads(p.read_text(encoding='utf-8')) for p in registry.glob('*.json')]
    record = next(r for r in records if r['container_id'] == postgres_test_db.container_id)
    target = tmp_path / 'record.json'
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    url = postgres_test_db.engine.url.render_as_string(hide_password=False)
    target.write_text(json.dumps({**record, 'run_token': '0' * 32}), encoding='utf-8')
    assert not is_isolated_startup_target(url)
    target.write_text(json.dumps({**record, 'pid': 99999999}), encoding='utf-8')
    assert not is_isolated_startup_target(url)
    assert record['process_identity']
    target.write_text(json.dumps({**record, 'process_identity': 'another-process'}), encoding='utf-8')
    assert not is_isolated_startup_target(url)
