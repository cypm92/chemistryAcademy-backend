"""The pytest entry point must not silently import the app on SQLite."""
import os
import json
from pathlib import Path
import subprocess
import sys
import pytest
import startup_postgres_guard as startup_guard
from startup_postgres_guard import _has_default_route, is_isolated_startup_target

ROOT = Path(__file__).resolve().parent.parent


def test_host_guard_rejects_relative_registry(tmp_path, monkeypatch):
    registry = tmp_path / 'relative-registry'
    registry.mkdir()
    token, container = 'a' * 32, 'b' * 64
    (registry / 'owner.json').write_text(json.dumps({
        'container_id': container, 'run_token': token, 'pid': 123,
        'process_identity': 'test-owner',
    }), encoding='utf-8')
    monkeypatch.setattr(startup_guard, 'owner_alive', lambda pid: True)
    monkeypatch.setattr(startup_guard, 'process_identity', lambda pid: 'test-owner')
    results = {'{{index .Config.Labels "beciencia.pytest.run"}}': token,
               '{{.State.Running}} {{.HostConfig.AutoRemove}}': 'true true',
               '{{json .HostConfig.Tmpfs}}': '{"/var/lib/postgresql/data": "rw"}',
               '{{json .NetworkSettings.Ports}}': '{"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}'}
    monkeypatch.setattr(startup_guard, '_docker_format', lambda container, template: results[template])
    url = 'postgresql://beciencia_test@127.0.0.1:5432/beciencia_test'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(registry))
    assert is_isolated_startup_target(url)
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', 'relative-registry')
    assert not is_isolated_startup_target(url)


def test_default_routes_cover_ipv4_and_ipv6(tmp_path):
    ipv4 = tmp_path / 'route'
    ipv6 = tmp_path / 'ipv6_route'
    ipv4.write_text('Iface Destination Gateway Flags\neth0 00000000 0100007F 0001\n', encoding='ascii')
    ipv6.write_text('', encoding='ascii')
    assert _has_default_route(ipv4, ipv6)
    ipv4.write_text('Iface Destination Gateway Flags\n', encoding='ascii')
    ipv6.write_text('00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 00000001 00000000 00000000 00000001 eth0\n', encoding='ascii')
    assert _has_default_route(ipv4, ipv6)
    ipv6.write_text('00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 00000001 00000000 00000000 00200200 lo\n', encoding='ascii')
    assert not _has_default_route(ipv4, ipv6)


@pytest.mark.parametrize('database_url', [None, 'sqlite://'])
def test_collection_rejects_non_postgres_before_importing_app(tmp_path, database_url):
    env = dict(os.environ, PYTHONPATH=str(ROOT), STORAGE_DIR=str(tmp_path / 'storage'))
    env.pop('DATABASE_URL', None)
    if database_url is not None:
        env['DATABASE_URL'] = database_url
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
                             '--rootdir=' + str(ROOT), '--collect-only', '-q',
                             str(ROOT / 'tests' / 'test_admin_reports.py')],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'PostgreSQL required for application tests' in result.stderr + result.stdout


@pytest.mark.parametrize('database_url', [None, 'sqlite://'])
def test_default_collection_rejects_non_postgres_before_importing_app(tmp_path, database_url):
    env = dict(os.environ, PYTHONPATH=str(ROOT), STORAGE_DIR=str(tmp_path / 'storage'))
    env.pop('DATABASE_URL', None)
    if database_url is not None:
        env['DATABASE_URL'] = database_url
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '--collect-only', '-q'],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'PostgreSQL required for application tests' in result.stderr + result.stdout


def test_collection_rejects_unattested_postgres_before_importing_app(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT), STORAGE_DIR=str(tmp_path / 'storage'),
               DATABASE_URL='postgresql://unused@198.51.100.1/production_like')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
                             '--rootdir=' + str(ROOT), '--collect-only', '-q',
                             str(ROOT / 'tests' / 'test_admin_reports.py')],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'isolated PostgreSQL' in result.stderr + result.stdout


def test_external_file_with_exempt_basename_does_not_bypass_guard(tmp_path):
    external = tmp_path / 'test_ephemeral_recovery.py'
    external.write_text('def test_external(): pass\n', encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop('DATABASE_URL', None)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
                             '--rootdir=' + str(ROOT), '--collect-only', '-q',
                             str(ROOT / 'tests' / 'test_ephemeral_recovery.py'), str(external)],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'PostgreSQL required for application tests' in result.stderr + result.stdout
