"""Attest the isolated PostgreSQL startup endpoint before application import."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from recover_ephemeral_postgres import owner_alive, process_identity, registry_path


def _docker_format(container, template):
    result = subprocess.run(['docker', 'inspect', '--format', template, container],
                            capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def _host_target(url):
    if url.host != '127.0.0.1' or url.username != 'beciencia_test' or url.database not in {'beciencia_test', 'app_startup_disposable'} or not url.port:
        return False
    registry = registry_path()
    for path in registry.glob('*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            container, token = record['container_id'], record['run_token']
            if not re.fullmatch(r'[0-9a-f]{64}', container) or not re.fullmatch(r'[0-9a-f]{32}', token):
                continue
            pid = record['pid']
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not owner_alive(pid):
                continue
            identity = record.get('process_identity')
            if not isinstance(identity, str) or not identity or process_identity(pid) != identity:
                continue
            label = _docker_format(container, '{{index .Config.Labels "beciencia.pytest.run"}}')
            state = _docker_format(container, '{{.State.Running}} {{.HostConfig.AutoRemove}}')
            mounts = json.loads(_docker_format(container, '{{json .HostConfig.Tmpfs}}') or '{}')
            ports = json.loads(_docker_format(container, '{{json .NetworkSettings.Ports}}') or '{}')
            bindings = ports.get('5432/tcp') or []
            if (label == token and state == 'true true' and '/var/lib/postgresql/data' in mounts
                    and any(b.get('HostIp') == '127.0.0.1' and b.get('HostPort') == str(url.port) for b in bindings)):
                return True
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            continue
    return False


def _has_default_route(ipv4_path, ipv6_path):
    ipv4_routes = ipv4_path.read_text(encoding='ascii').splitlines()[1:]
    if any(line.split()[1] == '00000000' for line in ipv4_routes if len(line.split()) > 1):
        return True
    for line in ipv6_path.read_text(encoding='ascii').splitlines():
        fields = line.split()
        if len(fields) < 10:
            raise ValueError('invalid IPv6 route')
        if fields[0] == '0' * 32 and fields[1] == '00' and int(fields[8], 16) & 1:
            return True
    return False


def _ci_target(url):
    if os.name != 'posix' or not Path('/.dockerenv').exists():
        return False
    if url.host != 'postgres' or url.username != 'ci_test' or url.database != 'app_startup_disposable' or url.port != 5432:
        return False
    identifier = os.environ.get('BECIENCIA_CI_PG_SYSTEM_ID', '')
    if not identifier.isdecimal():
        return False
    try:
        # These prechecks restrict possible connections, but do not attest server
        # identity: isolation relies on the freshly created CI network, and the
        # system identifier is checked only after connecting.
        if _has_default_route(Path('/proc/net/route'), Path('/proc/net/ipv6_route')):
            return False
        addresses = {item[4][0] for item in socket.getaddrinfo('postgres', 5432, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(addr).is_private for addr in addresses):
            return False
        engine = create_engine(url, connect_args={'connect_timeout': 3})
        try:
            with engine.connect() as connection:
                row = connection.execute(text('SELECT current_user, current_database(), system_identifier::text FROM pg_control_system()')).one()
            return tuple(row) == ('ci_test', 'app_startup_disposable', identifier)
        finally:
            engine.dispose()
    except Exception:
        return False


def is_isolated_startup_target(value):
    try:
        url = make_url(value)
        return not url.query and url.get_backend_name() == 'postgresql' and (_host_target(url) or _ci_target(url))
    except (ValueError, TypeError):
        return False
