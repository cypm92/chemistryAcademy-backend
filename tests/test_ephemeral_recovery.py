"""Recovery must only consider recorded resources of dead test processes."""
import json
import os
import subprocess
import sys
import pytest
import recover_ephemeral_postgres as recovery

from recover_ephemeral_postgres import owner_alive, recover_record


def test_owner_liveness_checks_a_live_and_exited_child(monkeypatch):
    if os.name == 'nt':
        def forbidden(*args):
            raise AssertionError('os.kill is not a Windows liveness probe')
        monkeypatch.setattr('recover_ephemeral_postgres.os.kill', forbidden)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        assert owner_alive(child.pid)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not owner_alive(child.pid)


def test_owner_with_exit_code_259_is_not_alive():
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.exit(259)'])
    child.wait(timeout=10)
    assert not owner_alive(child.pid)


@pytest.mark.parametrize(('error', 'expected'), [(87, False), (5, True)])
def test_windows_owner_liveness_open_errors(error, expected):
    class Kernel:
        def OpenProcess(self, access, inherit, pid):
            return None
    assert recovery._windows_owner_alive(123, Kernel(), lambda: error) is expected


@pytest.mark.parametrize(('wait_result', 'expected'), [(258, True), (0, False)])
def test_windows_owner_liveness_uses_process_signal_and_closes_handle(wait_result, expected):
    class Kernel:
        closed = []
        def OpenProcess(self, access, inherit, pid):
            return 42
        def WaitForSingleObject(self, handle, timeout):
            assert timeout == 0
            return wait_result
        def GetExitCodeProcess(self, handle, value):
            raise AssertionError('exit code 259 is ambiguous')
        def CloseHandle(self, handle):
            self.closed.append(handle)
    kernel = Kernel()
    assert recovery._windows_owner_alive(123, kernel, lambda: 0) is expected
    assert kernel.closed == [42]


def test_recovery_rejects_running_owner_without_inspecting_or_stopping(tmp_path):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': 'a' * 64, 'run_token': 'b' * 32, 'pid': os.getpid()}))
    calls = []
    result = recover_record(record, lambda *args: calls.append(args))
    assert result == 'owner-running'
    assert not calls
    assert record.exists()


def test_process_identity_distinguishes_live_children():
    first = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    second = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        first_id = recovery.process_identity(first.pid)
        second_id = recovery.process_identity(second.pid)
        assert first_id and second_id and first_id != second_id
        assert first_id == recovery.process_identity(first.pid)
    finally:
        for child in (first, second):
            child.terminate()
            child.wait(timeout=10)


def test_recovery_preserves_resource_when_recorded_pid_is_alive_even_if_birth_differs(tmp_path, monkeypatch):
    container, token = 'a' * 64, 'b' * 32
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': container, 'run_token': token,
                                  'pid': os.getpid(), 'process_identity': f'windows:{os.getpid()}:1'}))
    monkeypatch.setattr(recovery, 'process_identity', lambda pid: f'windows:{pid}:2')
    calls = []
    def docker(*args):
        calls.append(args)
        return token if args[0] == 'inspect' else ''
    assert recover_record(record, docker) == 'owner-running'
    assert not calls and record.exists()


def test_recovery_keeps_record_if_live_owner_identity_is_unavailable(tmp_path, monkeypatch):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': 'a' * 64, 'run_token': 'b' * 32,
                                  'pid': os.getpid(), 'process_identity': 'prior-owner'}))
    monkeypatch.setattr(recovery, 'process_identity', lambda pid: None)
    calls = []
    assert recover_record(record, lambda *args: calls.append(args)) == 'owner-running'
    assert not calls and record.exists()


def test_recovery_does_not_stop_live_owner_with_malformed_identity(tmp_path):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': 'a' * 64, 'run_token': 'b' * 32,
                                  'pid': os.getpid(), 'process_identity': 123}))
    calls = []
    result = recover_record(record, lambda *args: calls.append(args))
    assert result == 'owner-running'
    assert not calls and record.exists()


def test_recovery_does_not_stop_live_owner_with_invalid_text_identity(tmp_path):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': 'a' * 64, 'run_token': 'b' * 32,
                                  'pid': os.getpid(), 'process_identity': 'malformed'}))
    calls = []
    assert recover_record(record, lambda *args: calls.append(args)) == 'owner-running'
    assert not calls and record.exists()


def test_recovery_rejects_mismatched_container_identity(tmp_path, monkeypatch):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': 'a' * 64, 'run_token': 'b' * 32, 'pid': 12345}))
    monkeypatch.setattr('recover_ephemeral_postgres.owner_alive', lambda pid: False)
    calls = []
    def docker(*args):
        calls.append(args)
        return 'different-label'
    with pytest.raises(RuntimeError, match='identity'):
        recover_record(record, docker)
    assert not any(call[0] == 'stop' for call in calls)
    assert record.exists()


def test_recovery_preserves_pending_record_if_container_not_yet_found(tmp_path, monkeypatch):
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': None, 'run_token': 'b' * 32, 'pid': 12345}))
    monkeypatch.setattr(recovery, 'owner_alive', lambda pid: False)
    calls = []
    def docker(*args):
        calls.append(args)
        return ''
    assert recover_record(record, docker) == 'pending-not-found'
    assert record.exists() and not any(call[0] == 'stop' for call in calls)


def test_recovery_finds_pending_container_by_exact_name_and_checks_label(tmp_path, monkeypatch):
    token, container = 'b' * 32, 'a' * 64
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': None, 'run_token': token, 'pid': 12345}))
    monkeypatch.setattr(recovery, 'owner_alive', lambda pid: False)
    calls = []
    def docker(*args):
        calls.append(args)
        if args[0] == 'ps' and '--no-trunc' in args:
            return container
        if args[0] == 'inspect' and args[2] == '{{.State.Status}}':
            return 'running'
        if args[0] == 'inspect':
            return token
        return ''
    assert recover_record(record, docker) == 'removed'
    assert ('stop', '--time', '2', container) in calls
    assert not record.exists()


def test_pending_created_container_is_not_stopped_or_forgotten(tmp_path, monkeypatch):
    token, container = 'b' * 32, 'a' * 64
    record = tmp_path / 'x.json'
    record.write_text(json.dumps({'container_id': None, 'run_token': token, 'pid': 12345}))
    monkeypatch.setattr(recovery, 'owner_alive', lambda pid: False)
    calls = []
    def docker(*args):
        calls.append(args)
        if args[0] == 'ps':
            return container
        if args[2] == '{{.State.Status}}':
            return 'created'
        return token
    assert recover_record(record, docker) == 'pending-not-running'
    assert record.exists() and not any(call[0] == 'stop' for call in calls)


def test_recovery_stops_only_exact_id_and_removes_record_after_absence(tmp_path, monkeypatch):
    record = tmp_path / 'x.json'
    container = 'a' * 64
    token = 'b' * 32
    record.write_text(json.dumps({'container_id': container, 'run_token': token, 'pid': 12345}))
    monkeypatch.setattr('recover_ephemeral_postgres.owner_alive', lambda pid: False)
    calls = []
    def docker(*args):
        calls.append(args)
        if args[0] == 'inspect':
            return token
        if args[0] == 'ps':
            return ''
        return container
    assert recover_record(record, docker) == 'removed'
    assert ('stop', '--time', '2', container) in calls
    assert not record.exists()
