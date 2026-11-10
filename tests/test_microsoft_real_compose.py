from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_compose(name):
    return yaml.safe_load((ROOT / name).read_text(encoding='utf-8'))


def test_real_compose_is_separate_and_opt_in():
    fake = load_compose('docker-compose.local.yml')
    overlay = load_compose('docker-compose.microsoft-real.local.yml')
    assert set(overlay['services']) <= set(fake['services'])
    for name in ('backend', 'notification-worker'):
        e = {**fake['services'][name]['environment'], **overlay['services'][name]['environment']}
        assert e['DATABASE_URL'].endswith('/beciencia_microsoft_real_local')
        assert fake['services'][name]['environment']['DATABASE_URL'].endswith('/beciencia_local')
        assert e['SANDBOX_INJECT'] == '0'
        assert e['MICROSOFT_SYNC_ENABLED'] == 'false'
        assert e['MICROSOFT_REDIRECT_URI'] == 'http://localhost:5174/api/integrations/microsoft/callback'
        assert 'real-egress' in overlay['services'][name]['networks']
    assert overlay['services']['frontend']['environment']['VITE_MICROSOFT_SIMULATED'] == 'false'
    assert overlay['services']['frontend']['build']['args']['VITE_LOCAL_SANDBOX_BUILD'] == 'false'
    assert overlay['services']['frontend']['build']['args']['VITE_MICROSOFT_SIMULATED'] == 'false'
    assert overlay['services']['frontend']['image'] != fake['services']['frontend']['image']
    assert overlay['services']['gateway']['environment']['MICROSOFT_REAL_LOCAL'] == 'true'
    assert fake['services']['notification-worker']['environment']['SMTP_HOST'] == 'mailpit'
    assert fake['services']['notification-worker']['environment']['SMTP_PASSWORD'] == ''
    assert fake['services']['gateway']['ports'][0].startswith('127.0.0.1:')


def test_real_settings_are_required_by_compose():
    overlay = load_compose('docker-compose.microsoft-real.local.yml')
    env = overlay['services']['backend']['environment']
    for name in ('TENANT_ID', 'CLIENT_ID', 'CLIENT_SECRET', 'FERNET_KEY'):
        assert env['MICROSOFT_' + name].startswith('${REAL_MICROSOFT_' + name + ':?')
    assert overlay['services']['notification-worker']['environment'] == env


def test_real_storage_overrides_simulator_mount_by_target():
    fake = load_compose('docker-compose.local.yml')
    real = load_compose('docker-compose.microsoft-real.local.yml')
    assert 'beciencia_local_files:/app/storage' in fake['services']['backend']['volumes']
    assert 'beciencia_microsoft_real_local_files:/app/storage' in real['services']['backend']['volumes']
    assert 'beciencia_microsoft_real_local_files' in real['volumes']
