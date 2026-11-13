"""Check startup destination attestation against a real isolated container."""
import json
from ephemeral_postgres import postgres_test_db
from recover_ephemeral_postgres import registry_path
from startup_postgres_guard import is_isolated_startup_target


def test_startup_guard_checks_container_endpoint_and_disallows_url_overrides(postgres_test_db):
    url = postgres_test_db.engine.url
    assert is_isolated_startup_target(url.render_as_string(hide_password=False))
    assert not is_isolated_startup_target(url.set(port=url.port + 1).render_as_string(hide_password=False))
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
