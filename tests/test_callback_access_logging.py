import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def test_uvicorn_redacts_callback_query_but_not_other_requests(tmp_path):
    (tmp_path / 'fixture_app.py').write_text(
        "async def app(scope, receive, send):\n"
        "    assert scope['type'] == 'http'\n"
        "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': scope['query_string']})\n"
    )
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = ROOT / 'app' / 'uvicorn-log.json'
    command = [sys.executable, '-m', 'uvicorn', 'fixture_app:app', '--host', '127.0.0.1', '--port', str(port), '--no-use-colors']
    if config.exists():
        command += ['--log-config', str(config)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(tmp_path)]))
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for attempt in range(100):
            try:
                url = f'http://127.0.0.1:{port}'
                with urllib.request.urlopen(url + '/integrations/microsoft/callback?code=fake-code&state=fake-state') as response:
                    assert response.read() == b'code=fake-code&state=fake-state'
                break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError(proc.stdout.read())
                time.sleep(.05)
        else:
            raise AssertionError('Uvicorn did not start')
        with urllib.request.urlopen(url + '/other?visible=normal') as response:
            assert response.read() == b'visible=normal'
    finally:
        proc.terminate()
        output = proc.communicate(timeout=10)[0]
    assert 'fake-code' not in output
    assert 'fake-state' not in output
    assert '/integrations/microsoft/callback' in output
    assert '/other?visible=normal' in output


def test_nginx_callback_location_disables_access_logging_and_preserves_proxy():
    nginx_config = ROOT.parent / 'frontend' / 'nginx.conf'
    if not nginx_config.exists():
        raise unittest.SkipTest('frontend checkout is not present')
    config = nginx_config.read_text()
    marker = 'location = /api/integrations/microsoft/callback {'
    assert marker in config
    block = config.split(marker, 1)[1].split('}', 1)[0]
    assert 'access_log off;' in block
    assert 'proxy_pass http://backend:8000/integrations/microsoft/callback;' in block
    assert 'proxy_request_buffering off;' in block
    assert 'proxy_buffering off;' in block
