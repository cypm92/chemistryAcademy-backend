"""HTTP-only recreate smoke; local sandbox only, never resets existing data."""
import os
import http.cookiejar, json, re, time, uuid
import urllib.request, urllib.error, urllib.parse
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
def call(path, method='GET', data=None, token=None, form=False):
    assert path.startswith('/') and not path.startswith('//')
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    payload = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, method=method, headers=headers, data=payload)
    try: response = opener.open(req, timeout=30)
    except urllib.error.HTTPError as e: response = e
    return response.status, response.headers, response.read().decode()
def ok(path, method='GET', data=None, token=None, expected=200, form=False):
    status, headers, body = call(path, method, data, token, form)
    assert status == expected, (path, status, body)
    return headers, body
def events():
    _, html = ok('/fake/')
    return html, re.findall(r'name="delete_event" value="([^"]+)"', html)
def row():
    _, body = ok('/api/admin/classes', token=token)
    return next(x for x in json.loads(body) if x['id'] == booking_id)
def wait(predicate, label):
    deadline = time.monotonic() + 180
    r = None
    while time.monotonic() < deadline:
        r = row()
        if predicate(r): return r
        time.sleep(3)
    raise AssertionError((label, r))

_, body = ok('/api/auth/login', 'POST', {'email':ADMIN_EMAIL,'password':ADMIN_PASSWORD})
token = json.loads(body)['access_token']
_, body = ok('/api/admin/integrations/microsoft/start', 'POST', token=token)
url = json.loads(body)['authorization_url']; assert url.startswith(BASE + '/fake/')
_, body = ok(url[len(BASE):]); assert 'SIMULACIÓN LOCAL' in body
match = re.search(r'href="([^"]+)"', body); assert match
callback = unescape(match.group(1)); assert callback.startswith(BASE + '/api/integrations/microsoft/callback?')
headers, _ = ok(callback[len(BASE):], expected=303); assert headers['Location'] == '/admin?microsoft=connected'
_, body = ok('/api/admin/integrations/microsoft', token=token); assert json.loads(body)['connected'] is True
print('PASS login and fake OAuth state/PKCE reconnect', flush=True)
html, before = events(); assert 'Escenario pendiente: Normal' in html, 'A scenario is already pending; not overwriting it'
_, body = ok('/api/auth/register', 'POST', {'name':'Sandbox Recreate', 'email':f'sandbox-recreate-{uuid.uuid4().hex}@example.com', 'password':STUDENT_PASSWORD}, expected=201)
user_id = json.loads(body)['id']
ok('/fake/controls', 'POST', {'fault':'ambiguous_absent'}, expected=303, form=True)
html, _ = events(); assert 'Escenario pendiente: ambiguous_absent' in html
start = (datetime.now(timezone.utc) + timedelta(days=30 + uuid.uuid4().int % 200)).replace(hour=10, minute=0, second=0, microsecond=0)
for n in range(30):
    status, _, body = call('/api/admin/classes', 'POST', {'user_id':user_id,'starts_at':(start+timedelta(days=n)).isoformat(),'subject':'Química ESO','duration_slots':1,'admin_comment':'Prueba sintética de recreación'}, token)
    if status == 201: break
    assert status == 409, (status, body)
else: raise AssertionError('No free synthetic slot')
booking_id = json.loads(body)['id']; print('PASS confirmed synthetic booking', booking_id, flush=True)
r = wait(lambda x:x['teams_status']=='needs_review', 'needs_review')
assert r['teams_review_reason']=='creation_result_unknown', r
html, absent = events(); assert set(absent)==set(before), (before, absent)
assert 'Escenario pendiente: Normal' in html
print('PASS ambiguous absent, needs_review, no new Outlook event; revision', r['teams_revision'], flush=True)
revision=r['teams_revision']
status, _, body = call(f'/api/admin/classes/{booking_id}/teams/recreate','POST',{'expected_revision':revision,'confirm_absent':False},token); assert status==422, (status,body)
status, _, body = call(f'/api/admin/classes/{booking_id}/teams/recreate','POST',{'expected_revision':revision-1,'confirm_absent':True},token); assert status==409, (status,body)
_, body=ok(f'/api/admin/classes/{booking_id}/teams/recreate','POST',{'expected_revision':revision,'confirm_absent':True},token); assert json.loads(body)['status']=='queued'
print('PASS explicit confirmation/revision guards and recreate queued', flush=True)
r=wait(lambda x:x['teams_status']=='ready', 'ready')
assert 'Enlace de Teams:' in r['admin_comment'] and '/fake/meeting/' in r['admin_comment'], r
html, after=events(); new=set(after)-set(before); assert len(new)==1 and set(before).issubset(after), (before,after)
meeting=r['teams_join_url']; assert meeting.startswith(BASE+'/fake/meeting/'), meeting
_, body=ok(meeting[len(BASE):]); assert 'SIMULACIÓN LOCAL' in body
print('PASS ready, comment and working fake meeting; exactly one new event', new, 'unresolved incidents',r['teams_unresolved_attempts'], flush=True)
print('RESULT booking',booking_id,'meeting',meeting,'outlook',BASE+'/fake/', flush=True)
