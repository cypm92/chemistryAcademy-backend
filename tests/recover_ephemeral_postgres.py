"""Recover one explicitly selected, attributable PostgreSQL test container.

Never scan for or delete containers without an ownership record. Refuse to
act while the recorded owner is alive or the Docker identity cannot be proved.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def registry_path():
    path = Path(os.environ.get('BECIENCIA_PYTEST_REGISTRY')
                or Path.home() / '.cache' / 'beciencia-pytest-containers')
    if not path.is_absolute():
        raise ValueError('BECIENCIA_PYTEST_REGISTRY must be an absolute path')
    return path


def process_identity(pid):
    """Return process birth identity, or None if it cannot be established."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return f'windows:{pid}:{birth}'
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith('linux'):
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
            stat = Path(f'/proc/{pid}/stat').read_text(encoding='ascii')
            start = stat.rsplit(')', 1)[1].split()[19]
            return f'linux:{boot}:{pid}:{start}'
        except (OSError, IndexError):
            return None
    return None


def _windows_owner_alive(pid, kernel, get_last_error):
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        error = get_last_error()
        if error == 87:
            return False
        if error == 5:
            return True
        raise OSError(error, 'OpenProcess failed')
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        if status == 258:  # WAIT_TIMEOUT: process has not exited
            return True
        if status == 0:  # WAIT_OBJECT_0: process has exited
            return False
        raise OSError(get_last_error(), 'WaitForSingleObject failed')
    finally:
        kernel.CloseHandle(handle)


def owner_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        return _windows_owner_alive(pid, kernel, ctypes.get_last_error)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True



def docker_command(*args):
    try:
        return subprocess.run(['docker', *args], check=True, capture_output=True,
                              text=True, timeout=90).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Docker recovery operation failed') from None


def recover_record(path, docker=docker_command):
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    container = data.get('container_id')
    token = data.get('run_token')
    pid = data.get('pid')
    if container is not None and (not isinstance(container, str) or not re.fullmatch(r'[0-9a-f]{64}', container)):
        raise ValueError('invalid container ID')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('invalid run token')
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('invalid owner PID')
    if owner_alive(pid):
        # A changed or corrupted birth identity is indistinguishable from PID
        # reuse. Never stop an attributable resource while this PID is live.
        return 'owner-running'
    pending = container is None
    if pending:
        name = 'beciencia-pytest-' + token
        container = docker('ps', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
        if not container:
            return 'pending-not-found'
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise RuntimeError('pending container lookup is ambiguous')
    label = docker('inspect', '--format', '{{index .Config.Labels "beciencia.pytest.run"}}', container)
    if label != token:
        raise RuntimeError('container identity does not match ownership record')
    if pending and docker('inspect', '--format', '{{.State.Status}}', container) != 'running':
        return 'pending-not-running'
    docker('stop', '--time', '2', container)
    deadline = time.monotonic() + 10
    while docker('ps', '-a', '--filter', 'id=' + container, '--format', '{{.ID}}'):
        if time.monotonic() >= deadline:
            raise RuntimeError('container recovery cleanup failed')
        time.sleep(0.1)
    if json.loads(path.read_text(encoding='utf-8')) != data:
        raise RuntimeError('ownership record changed during recovery')
    path.unlink()
    return 'removed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record', type=Path, help='exact ownership record to recover')
    args = parser.parse_args()
    print(recover_record(args.record))


if __name__ == '__main__':
    main()
