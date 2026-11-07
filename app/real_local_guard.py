"""Fail closed before launching either process in the opt-in local profile."""
import base64
import binascii
import os
import sys
from uuid import UUID


def valid(env):
    try:
        ids = [UUID(env['MICROSOFT_TENANT_ID']), UUID(env['MICROSOFT_CLIENT_ID'])]
        if any(x.int == 0 or str(x) in ('00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000003') for x in ids):
            return False
        secret = env['MICROSOFT_CLIENT_SECRET'].strip()
        if not secret or any(word in secret.lower() for word in ('placeholder', 'sandbox', 'changeme', 'test-only')):
            return False
        key = base64.b64decode(env['MICROSOFT_FERNET_KEY'], altchars=b'-_', validate=True)
        return len(key) == 32 and len(set(key)) > 1
    except (KeyError, ValueError, TypeError, binascii.Error):
        return False


if __name__ == '__main__':
    if not valid(os.environ) or len(sys.argv) < 2:
        sys.exit('Invalid local Microsoft configuration; process not started')
    os.execvp(sys.argv[1], sys.argv[1:])
