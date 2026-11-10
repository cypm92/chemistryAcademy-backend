from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_local_application_uses_production_images_and_preserves_storage():
    config = yaml.safe_load((ROOT / 'docker-compose.local.yml').read_text(encoding='utf-8'))
    services = config['services']
    frontend = services['frontend']
    assert frontend['build']['context'] == '../frontend'
    assert frontend['build']['args']['VITE_LOCAL_SANDBOX_BUILD'] == 'true'
    assert 'command' not in frontend
    assert not frontend.get('volumes')
    backend = services['backend']
    assert '--reload' not in backend.get('command', [])
    assert './app:/app/app' not in backend['volumes']
    assert './app:/app/app' not in services['notification-worker']['volumes']
    assert 'beciencia_local_files:/app/storage' in backend['volumes']
    assert 'beciencia_local_db:/var/lib/postgresql/data' in services['db']['volumes']
    assert services['notification-worker']['image'] == backend['image']
    assert str(services['gateway']['environment']['SANDBOX_FRONTEND_PORT']) == '80'
    assert config['networks']['isolated']['internal'] is True
