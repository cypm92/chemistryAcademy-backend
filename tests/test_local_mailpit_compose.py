from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

def test_local_worker_uses_isolated_mailpit():
    config = yaml.safe_load((ROOT / 'docker-compose.local.yml').read_text(encoding='utf-8'))
    worker = config['services']['notification-worker']
    mail = config['services']['mailpit']
    assert worker['environment']['SMTP_MODE'] == 'smtp'
    assert worker['environment']['SMTP_HOST'] == 'mailpit'
    assert str(worker['environment']['SMTP_PORT']) == '1025'
    assert mail['image'] == 'axllent/mailpit:v1.28.2'
    assert not mail.get('ports')
    assert list(mail['networks']) == ['isolated']
    assert 'mailpit' in worker['depends_on']
    ui = config['services']['mailpit-ui']
    assert ui['ports'] == ['127.0.0.1:8025:8025']
    assert config['networks']['isolated']['internal'] is True
