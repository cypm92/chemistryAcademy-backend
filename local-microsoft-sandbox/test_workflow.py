import base64
import hashlib
import re
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from fastapi.testclient import TestClient
from server import create_app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def login(self):
        verifier = 'a' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': 'fake-client', 'response_type': 'code', 'redirect_uri': 'http://localhost:8000/callback', 'state': 'state123', 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        page = self.client.get('/tenant/oauth2/v2.0/authorize', params=params)
        self.assertEqual(page.status_code, 200)
        link = re.search(r'href="([^"]+)', page.text).group(1).replace('&amp;', '&')
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(query['state'], ['state123'])
        form = {'grant_type': 'authorization_code', 'client_id': 'fake-client', 'redirect_uri': params['redirect_uri'], 'code': query['code'][0], 'code_verifier': verifier}
        response = self.client.post('/tenant/oauth2/v2.0/token', data=form)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/tenant/oauth2/v2.0/token', data=form).status_code, 400)
        return response.json()

    def test_oauth_refresh_and_unsafe_redirect(self):
        tokens = self.login()
        self.assertEqual(self.client.get('/v1.0/me', headers={'Authorization': 'Bearer ' + tokens['access_token']}).json()['displayName'], 'Sandbox User')
        form = {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}
        self.assertEqual(self.client.post('/tenant/oauth2/v2.0/token', data=form).status_code, 200)
        self.assertEqual(self.client.post('/tenant/oauth2/v2.0/token', data=form).status_code, 400)
        self.assertEqual(self.client.get('/tenant/oauth2/v2.0/authorize', params={'redirect_uri': 'https://evil.example'}).status_code, 400)
        self.assertEqual(self.client.get('/v1.0/me', headers={'X-Sandbox-Original-Host': 'evil.example'}).status_code, 400)

    def test_ambiguous_absent_leaves_no_event_and_is_one_shot(self):
        headers = {'Authorization': 'Bearer ' + self.login()['access_token']}
        event = {'subject': 'Absent', 'transactionId': 'absent-tx'}
        self.assertEqual(self.client.post('/controls', data={'fault': 'ambiguous_absent'}).status_code, 200)
        self.assertEqual(self.client.get('/v1.0/me/events/not-found', headers=headers).status_code, 404)
        self.assertEqual(self.client.post('/v1.0/me/events', headers=headers, json=event).status_code, 503)
        self.assertNotIn('Absent', self.client.get('/').text)
        self.assertEqual(self.client.post('/v1.0/me/events', headers=headers, json=event).status_code, 201)

    def test_outlook_deletes_only_selected_fake_event(self):
        headers = {'Authorization': 'Bearer ' + self.login()['access_token']}
        first = self.client.post('/v1.0/me/events', headers=headers, json={'subject': 'First', 'transactionId': 'one'}).json()
        second = self.client.post('/v1.0/me/events', headers=headers, json={'subject': 'Second', 'transactionId': 'two'}).json()
        self.assertIn('Eliminar evento ficticio', self.client.get('/').text)
        self.assertEqual(self.client.post('/controls', data={'delete_event': first['id']}).status_code, 200)
        self.assertEqual(self.client.get('/v1.0/me/events/' + first['id'], headers=headers).status_code, 404)
        self.assertEqual(self.client.get('/v1.0/me/events/' + second['id'], headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/v1.0/me/events', headers=headers, json={'subject': 'First', 'transactionId': 'one'}).status_code, 201)

    def test_events_pagination_faults_and_visual_view(self):
        headers = {'Authorization': 'Bearer ' + self.login()['access_token']}
        event = {'subject': '<Class>', 'start': {'dateTime': '2026-11-01T10:00:00', 'timeZone': 'UTC'}, 'end': {'dateTime': '2026-11-01T11:00:00', 'timeZone': 'UTC'}, 'isOnlineMeeting': True, 'transactionId': 'tx1'}
        created = self.client.post('/v1.0/me/events', headers=headers, json=event)
        self.assertEqual(created.status_code, 201)
        item = created.json()
        self.assertIn('SANDBOX-NOT-A-REAL-MEETING', item['onlineMeeting']['joinUrl'])
        self.assertEqual(self.client.post('/v1.0/me/events', headers=headers, json=event).json()['id'], item['id'])
        event['transactionId'] = 'tx2'
        self.client.post('/controls', data={'fault': 'ambiguous'})
        self.assertEqual(self.client.post('/v1.0/me/events', headers=headers, json=event).status_code, 503)
        params = {'startDateTime': '2026-11-01T00:00:00Z', 'endDateTime': '2026-11-02T00:00:00Z', '$top': '1'}
        page = self.client.get('/v1.0/me/calendarView', headers=headers, params=params).json()
        next_link = urlsplit(page['@odata.nextLink'])
        self.assertEqual(next_link.hostname, 'graph.microsoft.com')
        self.assertEqual(len(self.client.get(next_link.path + '?' + next_link.query, headers=headers).json()['value']), 1)
        view = self.client.get('/').text
        self.assertIn('&lt;Class&gt;', view)
        self.assertNotIn('href="https://teams.microsoft.com', view)
        path = '/v1.0/me/events/' + item['id']
        self.assertEqual(self.client.patch(path, headers=headers, json={'subject': 'Changed'}).json()['subject'], 'Changed')
        self.assertEqual(self.client.get(path, headers=headers).json()['subject'], 'Changed')
        self.client.post('/controls', data={'fault': '429'})
        r = self.client.get(path, headers=headers)
        self.assertEqual((r.status_code, r.headers['retry-after']), (429, '2'))
        self.client.post('/controls', data={'fault': '404'})
        self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 204)
        self.assertEqual(self.client.get(path, headers=headers).status_code, 404)

    def test_actual_loopback_http(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with urlopen('http://127.0.0.1:%d/' % port, timeout=1) as response:
                        self.assertIn('Outlook ficticio', response.read().decode())
                    break
                except OSError:
                    time.sleep(.05)
            else:
                self.fail('local HTTP server did not start')
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
