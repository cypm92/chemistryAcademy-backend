"""Exercise a synthetic booking lifecycle against the local sandbox only.

Requires the sandbox described in RUN.md to be running and connected. Never
changes simulator controls or deletes any booking/event except its own event
indirectly through the admin cancellation API.
"""
import os
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape

BASE = 'http://127.0.0.1:5174'
ADMIN_EMAIL = os.environ.get('SMOKE_ADMIN_EMAIL')
ADMIN_PASSWORD = os.environ.get('SMOKE_ADMIN_PASSWORD')
if not ADMIN_EMAIL or not ADMIN_PASSWORD:
    raise SystemExit('Set SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PASSWORD for a local dummy admin')
STUDENT_PASSWORD = 'LocalDummy-' + uuid.uuid4().hex
PASSWORD = STUDENT_PASSWORD


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())


def call(path, method='GET', data=None, token=None):
    assert path.startswith('/') and not path.startswith('//'), path
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(BASE + path, method=method, headers=headers,
        data=json.dumps(data).encode() if data is not None else None)
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    return response.status, response.read().decode()


def ok(path, method='GET', data=None, token=None, expected=200):
    status, body = call(path, method, data, token)
    assert status == expected, (path, status, body)
    return body


def events():
    html = ok('/fake/')
    assert 'SIMULACIÓN LOCAL' in html and 'Escenario pendiente: Normal' in html, 'Sandbox missing or fault pending'
    rows = re.findall(r'<li><strong>(.*?)</strong> — (.*?) — Teams ficticio.*?name="delete_event" value="([^"]+)"', html)
    return {unescape(eid): (unescape(subject), unescape(start)) for subject, start, eid in rows}


def instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def wait_for(label, fn):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(3)
    raise AssertionError('Timed out waiting for ' + label)


def main():
    before = events()
    token = json.loads(ok('/api/auth/login', 'POST', {'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD}))['access_token']
    integration = json.loads(ok('/api/admin/integrations/microsoft', token=token))
    assert integration['connected'] is True, 'Connect fake Microsoft account in sandbox first'
    suffix = uuid.uuid4().hex
    email = f'sandbox-lifecycle-{suffix}@example.com'
    comment = 'Comentario sintético lifecycle ' + suffix
    user = json.loads(ok('/api/auth/register', 'POST', {'name': 'Sandbox Lifecycle', 'email': email, 'password': PASSWORD}, expected=201))
    base = (datetime.now(timezone.utc) + timedelta(days=30 + uuid.uuid4().int % 200)).replace(hour=10, minute=0, second=0, microsecond=0)
    for day in range(30):
        initial = base + timedelta(days=day)
        status, body = call('/api/admin/classes', 'POST', {'user_id': user['id'], 'starts_at': initial.isoformat(), 'subject': 'Química ESO', 'duration_slots': 1, 'admin_comment': comment}, token)
        if status == 201:
            break
        assert status == 409, (status, body)
    else:
        raise AssertionError('No free initial slot')
    booking = json.loads(body)
    bid = booking['id']
    assert booking['status'] == 'confirmed'
    print(f'PASS synthetic confirmed booking {bid}', flush=True)

    def row():
        rows = json.loads(ok('/api/admin/classes', token=token))
        return next(x for x in rows if x['id'] == bid)

    def created():
        current = row()
        matches = {eid: v for eid, v in events().items() if email in v[0]}
        if current['teams_status'] == 'ready' and len(matches) == 1:
            return current, matches
        return None

    current, matches = wait_for('initial event and Teams link', created)
    event_id = next(iter(matches))
    assert event_id not in before and instant(matches[event_id][1]) == initial
    assert set(before).issubset(events())
    link = current['teams_join_url']
    assert link.startswith(BASE + '/fake/meeting/') and link in current['admin_comment'] and comment in current['admin_comment']
    assert 'SIMULACIÓN LOCAL' in ok(link[len(BASE):])
    print(f'PASS fake event {event_id} and meeting created', flush=True)

    for day in range(31, 61):
        changed = base + timedelta(days=day)
        status, body = call(f'/api/admin/classes/{bid}', 'PATCH', {'starts_at': changed.isoformat(), 'subject': 'Química ESO', 'duration_slots': 2, 'admin_comment': current['admin_comment']}, token)
        if status == 200:
            break
        assert status == 409, (status, body)
    else:
        raise AssertionError('No free reschedule slot')
    updated = json.loads(body)
    assert updated['id'] == bid and instant(updated['starts_at']) == changed and instant(updated['ends_at']) == changed + timedelta(hours=1)

    def rescheduled():
        r = row()
        ev = events()
        if r['teams_status'] == 'ready' and event_id in ev and instant(ev[event_id][1]) == changed:
            return r, ev
        return None

    current, after = wait_for('rescheduled fake event', rescheduled)
    assert current['id'] == bid and instant(current['starts_at']) == changed and instant(current['ends_at']) == changed + timedelta(hours=1)
    assert current['teams_join_url'] == link and link in current['admin_comment'] and comment in current['admin_comment']
    assert set(before).issubset(after) and {eid for eid, v in after.items() if email in v[0]} == {event_id}
    print(f'PASS rescheduled same booking and event; {initial.isoformat()} -> {changed.isoformat()}, comment and link preserved', flush=True)

    cancelled = json.loads(ok(f'/api/admin/classes/{bid}/cancel', 'POST', token=token))
    assert cancelled['id'] == bid and cancelled['status'] == 'cancelled'

    def deleted():
        r, ev = row(), events()
        if r['teams_status'] is None and event_id not in ev:
            return r, ev
        return None

    current, after = wait_for('cancelled event deletion', deleted)
    assert current['status'] == 'cancelled' and current['teams_join_url'] is None
    assert set(before).issubset(after) and not any(email in v[0] for v in after.values())
    status, _ = call(link[len(BASE):])
    assert status == 404, status
    print('PASS cancelled: fake event and meeting removed; pre-existing events preserved', flush=True)
    assert link not in current['admin_comment'] and 'Enlace de Teams:' not in current['admin_comment'] and comment in current['admin_comment'], (current['admin_comment'], link)
    print('PASS original comment retained and Teams link removed', flush=True)
    print(f'RESULT booking={bid} event={event_id}', flush=True)


if __name__ == '__main__':
    main()
