"""Integration test for a dedicated PostgreSQL container per test."""
import subprocess
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest
from sqlalchemy import text
import ephemeral_postgres as ephemeral_module
from ephemeral_postgres import ephemeral_postgres
from recover_ephemeral_postgres import owner_alive, recover_record


def test_postgres_is_isolated_and_allows_independent_connections():
    with ephemeral_postgres() as first, ephemeral_postgres() as second:
        assert first.container_id != second.container_id
        with first.engine.begin() as conn:
            conn.execute(text('CREATE TABLE sample (value integer)'))
            conn.execute(text('INSERT INTO sample VALUES (7)'))
        with first.engine.connect() as a, first.engine.connect() as b:
            assert a.execute(text('SELECT value FROM sample')).scalar_one() == 7
            assert b.execute(text('SELECT value FROM sample')).scalar_one() == 7
            assert a.connection.driver_connection is not b.connection.driver_connection
        with second.engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('public.sample')")).scalar() is None


def test_case_session_factory_preserves_sqlalchemy_autoflush_default():
    with ephemeral_postgres() as database:
        assert database.session_factory.kw['autoflush'] is True


def test_container_is_removed_when_test_raises():
    container_id = None
    with pytest.raises(ValueError, match='test failure'):
        with ephemeral_postgres() as database:
            container_id = database.container_id
            raise ValueError('test failure')
    assert container_id is not None
    result = subprocess.run(['docker', 'ps', '-a', '--filter', 'id=' + container_id,
                             '--format', '{{.ID}}'], capture_output=True, text=True, check=True)
    assert not result.stdout.strip()


def test_already_removed_container_does_not_mask_test_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    with pytest.raises(ValueError, match='original failure'):
        with ephemeral_postgres() as database:
            subprocess.run(['docker', 'stop', '--time', '2', database.container_id],
                           check=True, capture_output=True)
            raise ValueError('original failure')
    assert not list(tmp_path.glob('*.json'))


def test_cleanup_failure_preserves_original_error_and_record(tmp_path, monkeypatch):
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    real_docker = ephemeral_module._docker
    container_id = None
    try:
        with pytest.raises(ValueError, match='original failure') as caught:
            with ephemeral_postgres() as database:
                container_id = database.container_id
                def fail_stop(*args, **kwargs):
                    if args[0] == 'stop':
                        raise RuntimeError('simulated cleanup error')
                    return real_docker(*args, **kwargs)
                monkeypatch.setattr(ephemeral_module, '_docker', fail_stop)
                raise ValueError('original failure')
        assert any('cleanup also failed' in note for note in caught.value.__notes__)
        records = list(tmp_path.glob('*.json'))
        assert len(records) == 1
        assert json.loads(records[0].read_text(encoding='utf-8'))['container_id'] == container_id
    finally:
        monkeypatch.setattr(ephemeral_module, '_docker', real_docker)
        if container_id is not None:
            real_docker('stop', '--time', '2', container_id)
            deadline = time.monotonic() + 10
            while real_docker('ps', '-a', '--filter', 'id=' + container_id, '--format', '{{.ID}}'):
                assert time.monotonic() < deadline
                time.sleep(0.1)


def test_container_ownership_record_survives_until_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    with ephemeral_postgres() as database:
        records = list(tmp_path.glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        assert record['container_id'] == database.container_id
        assert record['pid'] > 0
        assert record['run_token']
        assert 'password' not in record
    assert not list(tmp_path.glob('*.json'))


def test_pending_ownership_is_persisted_before_docker_run(tmp_path, monkeypatch):
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    def interrupted(*args, **kwargs):
        assert args[0] == 'run'
        records = list(tmp_path.glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        assert record['run_token']
        assert record['pid'] == os.getpid()
        assert record['process_identity']
        assert record['container_id'] is None
        raise RuntimeError('simulated interrupted docker run')
    monkeypatch.setattr(ephemeral_module, '_docker', interrupted)
    with pytest.raises(RuntimeError, match='interrupted docker run'):
        with ephemeral_postgres():
            pass
    assert len(list(tmp_path.glob('*.json'))) == 1


def test_empty_registry_variable_never_uses_temporary_working_directory(tmp_path, monkeypatch):
    home, work = tmp_path / 'home', tmp_path / 'work'
    home.mkdir()
    work.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.chdir(work)
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', '')
    def interrupted(*args, **kwargs):
        assert list((home / '.cache' / 'beciencia-pytest-containers').glob('*.json'))
        assert not list(work.glob('*.json'))
        raise RuntimeError('simulated interruption')
    monkeypatch.setattr(ephemeral_module, '_docker', interrupted)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        with ephemeral_postgres():
            pass


def test_relative_registry_is_rejected_before_docker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', 'relative-registry')
    calls = []
    monkeypatch.setattr(ephemeral_module, '_docker', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match='absolute'):
        with ephemeral_postgres():
            pass
    assert not calls
    assert not (tmp_path / 'relative-registry').exists()


def test_failed_record_replace_cleans_temporary_file_after_container_removed(tmp_path, monkeypatch):
    monkeypatch.setenv('BECIENCIA_PYTEST_REGISTRY', str(tmp_path))
    container = 'a' * 64
    def docker(*args, **kwargs):
        if args[0] == 'run':
            return container
        return ''
    def fail_replace(*args):
        raise OSError('simulated replace failure')
    monkeypatch.setattr(ephemeral_module, '_docker', docker)
    monkeypatch.setattr(ephemeral_module.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='simulated replace failure'):
        with ephemeral_postgres():
            pass
    assert not list(tmp_path.iterdir())


def test_pending_record_recovers_container_after_interruption(tmp_path):
    registry = Path.home() / '.cache' / 'beciencia-pytest-integration' / uuid.uuid4().hex
    ready = tmp_path / 'ready'
    code = '''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ephemeral_postgres as module
original = module._docker
def pause_after_run(*args, **kwargs):
    result = original(*args, **kwargs)
    if args[0] == 'run':
        Path(sys.argv[2]).write_text(json.dumps({'container_id': result, 'pid': os.getpid()}))
        time.sleep(120)
    return result
module._docker = pause_after_run
with module.ephemeral_postgres():
    pass
'''
    env = os.environ.copy()
    env['BECIENCIA_PYTEST_REGISTRY'] = str(registry)
    env['PYTHONPATH'] = os.pathsep.join([str(Path(pytest.__file__).parent.parent), env.get('PYTHONPATH', '')])
    child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', code,
                              str(Path(__file__).parent), str(ready)], env=env)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists()
        records = list(registry.glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        started = json.loads(ready.read_text(encoding='utf-8'))
        assert started['pid'] == child.pid
        assert record['container_id'] is None and record['pid'] == started['pid']
        container = started['container_id']
        child.kill()
        child.wait(timeout=10)
        deadline = time.monotonic() + 10
        while owner_alive(started['pid']) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert recover_record(records[0]) == 'removed'
        assert not records[0].exists()
        assert not ephemeral_module._docker('ps', '-a', '--filter', 'id=' + container, '--format', '{{.ID}}')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        records = list(registry.glob('*.json')) if registry.exists() else []
        if len(records) == 1:
            remaining = json.loads(records[0].read_text(encoding='utf-8'))
            if remaining['pid'] == child.pid:
                recover_record(records[0])
        if registry.exists() and not any(registry.iterdir()):
            registry.rmdir()


def test_pending_record_survives_owner_killed_during_docker_run(tmp_path):
    registry = Path.home() / '.cache' / 'beciencia-pytest-integration' / uuid.uuid4().hex
    ready = tmp_path / 'ready'
    code = '''
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ephemeral_postgres as module
original = module._docker
def intercept(*args, **kwargs):
    if args[0] != 'run':
        return original(*args, **kwargs)
    process = subprocess.Popen(['docker', *args], env=kwargs['env'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    Path(sys.argv[2]).write_text(json.dumps({'owner': os.getpid(), 'docker_pid': process.pid}))
    out, err = process.communicate(timeout=90)
    if process.returncode:
        raise RuntimeError('docker run failed')
    return out.decode().strip()
module._docker = intercept
with module.ephemeral_postgres():
    pass
'''
    env = os.environ.copy()
    env['BECIENCIA_PYTEST_REGISTRY'] = str(registry)
    env['PYTHONPATH'] = os.pathsep.join([str(Path(pytest.__file__).parent.parent), env.get('PYTHONPATH', '')])
    python = getattr(sys, '_base_executable', sys.executable)
    child = subprocess.Popen([python, '-c', code, str(Path(__file__).parent), str(ready)], env=env)
    records = []
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists()
        started = json.loads(ready.read_text(encoding='utf-8'))
        assert started['owner'] == child.pid
        records = list(registry.glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        assert record['pid'] == child.pid and record['container_id'] is None
        assert owner_alive(started['docker_pid']), 'docker client already exited before interruption'
        child.kill()
        child.wait(timeout=10)
        deadline = time.monotonic() + 90
        result = 'pending-not-found'
        while time.monotonic() < deadline:
            result = recover_record(records[0])
            if result == 'removed':
                break
            assert result in {'pending-not-found', 'pending-not-running'}
            time.sleep(0.2)
        assert result == 'removed', 'pending record retained because container was not attributable'
        assert not records[0].exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        records = list(registry.glob('*.json')) if registry.exists() else []
        if len(records) == 1:
            record = json.loads(records[0].read_text(encoding='utf-8'))
            if record['pid'] == child.pid:
                recover_record(records[0])
        if registry.exists() and not any(registry.iterdir()):
            registry.rmdir()


def test_ownership_record_survives_process_termination(tmp_path):
    registry = Path.home() / '.cache' / 'beciencia-pytest-integration' / uuid.uuid4().hex
    ready = tmp_path / 'ready'
    code = (
        'import sys,time; from pathlib import Path; '
        'sys.path.insert(0, sys.argv[1]); '
        'from ephemeral_postgres import ephemeral_postgres; '
        'ctx=ephemeral_postgres(); db=ctx.__enter__(); '
        'Path(sys.argv[2]).write_text(db.container_id); time.sleep(120)'
    )
    env = os.environ.copy()
    env['BECIENCIA_PYTEST_REGISTRY'] = str(registry)
    env['PYTHONPATH'] = os.pathsep.join([str(Path(pytest.__file__).parent.parent), env.get('PYTHONPATH', '')])
    child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', code,
                              str(Path(__file__).parent), str(ready)], env=env)
    record = None
    records = []
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists(), 'child did not create its PostgreSQL container'
        records = list(registry.glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        assert record['container_id'] == ready.read_text(encoding='utf-8')
        child.kill()
        child.wait(timeout=10)
        assert records[0].exists()
        assert recover_record(records[0]) == 'removed'
        record = None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if record is None:
            records = list(registry.glob('*.json'))
            if len(records) == 1:
                record = json.loads(records[0].read_text(encoding='utf-8'))
                assert record['pid'] == child.pid
        if record is not None:
            assert len(records) == 1
            assert record['pid'] == child.pid
            result = recover_record(records[0])
            assert result in {'removed', 'pending-not-found', 'pending-not-running'}
        if registry.exists() and not any(registry.iterdir()):
            registry.rmdir()
