import os
import subprocess

def test_local_notification_recipient_override():
    env = dict(os.environ, NOTIFICATION_ADMIN_RECIPIENTS='preview@example.com')
    subprocess.run(['python', '-c', 'from app.notifications import DEFAULT_RECIPIENTS; assert DEFAULT_RECIPIENTS == ["preview@example.com"]'], env=env, check=True)
