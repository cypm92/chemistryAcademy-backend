"""Smoke the real API, browser OAuth callback and simulator over loopback only."""
import os
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape

BASE = 'http://127.0.0.1:5174'
ADMIN_EMAIL = os.environ.get('SMOKE_ADMIN_EMAIL')
ADMIN_PASSWORD = os.environ.get('SMOKE_ADMIN_PASSWORD')
if not ADMIN_EMAIL or not ADMIN_PASSWORD:
    raise SystemExit('Set SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PASSWORD for a local dummy admin')
STUDENT_PASSWORD = 'LocalDummy-' + uuid.uuid4().hex
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
def call(path, method='GET', data=None, token=None):
    assert path.startswith('/') and not path.startswith('//')
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    try: result = opener.open(req, timeout=30)
    except urllib.error.HTTPError as exc: result = exc
    return result.status, result.headers, result.read().decode()

status, _, body = call('/api/auth/login', 'POST', {'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD})
assert status == 200, (status, body)
token = json.loads(body)['access_token']
status, _, body = call('/api/admin/integrations/microsoft/start', 'POST', token=token)
assert status == 200, (status, body)
authorize = json.loads(body)['authorization_url']
assert authorize.startswith(BASE + '/fake/'), authorize
status, _, body = call(authorize[len(BASE):])
assert status == 200 and 'SIMULACIÓN LOCAL' in body
callback = unescape(re.search(r'href="([^"]+)"', body).group(1))
assert callback.startswith(BASE + '/api/integrations/microsoft/callback?')
status, headers, body = call(callback[len(BASE):])
assert status == 303 and headers['Location'] == '/admin?microsoft=connected', (status, headers.get('Location'), body)
status, _, body = call('/api/admin/integrations/microsoft', token=token)
assert status == 200 and json.loads(body)['connected'] is True, (status, body)
for path in ('/', '/fake/'):
    status, _, body = call(path)
    assert status == 200 and 'SIMULACIÓN LOCAL' in body, (path, status)
print('PASS: login, OAuth state/PKCE/callback, fake Graph identity, connected status, both watermarks')
status, _, body = call('/api/auth/register', 'POST', {'name':'Sandbox Student', 'email':f'sandbox-{uuid.uuid4().hex}@example.com', 'password':STUDENT_PASSWORD})
assert status == 201, (status, body)
user_id = json.loads(body)['id']
start = (datetime.now(timezone.utc) + timedelta(days=10 + uuid.uuid4().int % 300)).replace(hour=10, minute=0, second=0, microsecond=0)
status, _, body = call('/api/admin/classes', 'POST', {'user_id':user_id, 'starts_at':start.isoformat(), 'subject':'Química ESO', 'duration_slots':1, 'admin_comment':''}, token)
assert status == 201, (status, body)
booking_id = json.loads(body)['id']
for _ in range(35):
    time.sleep(3)
    status, _, body = call('/api/admin/classes', token=token)
    if status == 200:
        entry = next((x for x in json.loads(body) if x['id'] == booking_id), {})
        match = re.search(r'http://127\.0\.0\.1:5174/fake/meeting/[A-Za-z0-9-]+', json.dumps(entry))
        if match: break
else: raise AssertionError('Worker did not create a local meeting for booking %s: %s' % (booking_id, entry))
status, _, body = call(match.group(0)[len(BASE):])
assert status == 200 and 'SIMULACIÓN LOCAL' in body, (status, body)
print(f'PASS: student {user_id}, booking {booking_id}, real worker sync, local fake meeting')
