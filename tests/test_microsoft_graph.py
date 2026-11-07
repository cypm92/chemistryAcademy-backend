import json
import unittest
from datetime import datetime, timezone, timedelta

import httpx

from app.microsoft_graph import create_event
import app.microsoft_graph as graph


class MicrosoftGraphTest(unittest.TestCase):
    def test_all_event_requests_prefer_immutable_ids(self):
        requests = []
        start = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        join_url = 'https://teams.microsoft.com/meeting'

        def handler(request):
            requests.append(request)
            if request.method == 'POST':
                return httpx.Response(201, json={'id': 'immutable-id', 'onlineMeeting': {'joinUrl': join_url}})
            if request.method == 'DELETE':
                return httpx.Response(204)
            return httpx.Response(200, json={
                'subject': 'subject', 'start': {'dateTime': '2030-01-01T10:00:00', 'timeZone': 'UTC'},
                'end': {'dateTime': '2030-01-01T11:00:00', 'timeZone': 'UTC'},
                'onlineMeeting': {'joinUrl': join_url},
            })

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(graph.create_event(client, 'token', 'subject', start, end, 'tx'), ('immutable-id', join_url))
            self.assertEqual(graph.update_event(client, 'token', 'immutable-id', 'subject', start, end), join_url)
            self.assertEqual(graph.get_event(client, 'token', 'immutable-id'), join_url)
            self.assertEqual(graph.get_event_state(client, 'token', 'immutable-id'), ('subject', start, end, join_url))
            self.assertIsNone(graph.delete_event(client, 'token', 'immutable-id'))

        self.assertEqual([request.method for request in requests], ['POST', 'PATCH', 'GET', 'GET', 'DELETE'])
        for index, request in enumerate(requests):
            with self.subTest(method=request.method, index=index):
                self.assertIn('IdType="ImmutableId"', request.headers.get('Prefer', ''))
                if index == 3:
                    self.assertIn('outlook.timezone="UTC"', request.headers['Prefer'])

    def test_create_preserves_id_when_join_url_is_unsafe_or_missing(self):
        start = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
        for meeting in ({'joinUrl': 'https://evil.test/secret'}, {'joinUrl': 12}, None, {}, []):
            with self.subTest(meeting=meeting), httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(201, json={'id': 'created-id', 'onlineMeeting': meeting}))) as client:
                self.assertEqual(create_event(client, 'token', 'subject', start,
                                              start + timedelta(hours=1), 'transaction'), ('created-id', None))

    def test_update_rejects_untrusted_url_without_leaking_it(self):
        start = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
        url = 'https://evil.test/secret'
        with httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'onlineMeeting': {'joinUrl': url}}))) as client:
            with self.assertRaises(graph.GraphRequestError) as caught:
                graph.update_event(client, 'token', 'event-id', 'subject', start, start + timedelta(hours=1))
        self.assertNotIn(url, str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_get_event_returns_join_url_and_authorizes_encoded_request(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/l/meetup-join/x'}})
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            self.assertEqual(graph.get_event(client, 'secret-token', 'id/with?#'), 'https://teams.microsoft.com/l/meetup-join/x')
        self.assertEqual(seen[0].method, 'GET')
        self.assertEqual(seen[0].url.raw_path, b'/v1.0/me/events/id%2Fwith%3F%23')
        self.assertEqual(seen[0].headers['authorization'], 'Bearer secret-token')
        self.assertEqual(seen[0].content, b'')
    def test_get_event_absent_and_malformed_payloads(self):
        for payload in ({}, {'onlineMeeting': None}, {'onlineMeeting': {}}, {'onlineMeeting': {'joinUrl': None}}):
            with self.subTest(payload=payload), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
                self.assertIsNone(graph.get_event(client, 'secret-token', 'id'))
        for payload in ([], None, {'onlineMeeting': []}, {'onlineMeeting': False}, {'onlineMeeting': {'joinUrl': 1}}):
            with self.subTest(payload=payload), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
                with self.assertRaises(graph.GraphRequestError):
                    graph.get_event(client, 'secret-token', 'id')
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='secret invalid json'))) as client:
            with self.assertRaises(graph.GraphRequestError) as caught:
                graph.get_event(client, 'secret-token', 'id')
            self.assertNotIn('secret', str(caught.exception))

    def test_get_event_rejects_untrusted_urls(self):
        urls = ('http://teams.microsoft.com/x', 'https://teams.microsoft.com.evil.test/x',
                'https://evil.test/teams.microsoft.com/x', 'https://teams.microsoft.com@evil.test/x',
                'https://evil.test@teams.microsoft.com/x', 'https://teams.microsoft.com:444/x',
                'https://127.0.0.1/x', 'https://[::1]/x', 'https://sub.teams.microsoft.com/x',
                'https://teams.microsoft.com\\\\evil.test/x', 'https://teams.microsoft.com/\nsecret',
                'https://teams.microsoft.com', '', 'javascript:secret')
        for url in urls:
            with self.subTest(url=url), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'onlineMeeting': {'joinUrl': url}}))) as client:
                with self.assertRaises(graph.GraphRequestError) as caught:
                    graph.get_event(client, 'secret-token', 'secret-id')
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(caught.exception.status_code, 200)

    def test_get_event_errors_do_not_leak_and_do_not_follow_redirects(self):
        for status in (302, 404, 429, 500):
            with self.subTest(status=status):
                def handler(request):
                    return httpx.Response(status, text='secret response', headers={'Location': 'https://evil.test/', 'Retry-After': '120'})
                with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                    with self.assertRaises(graph.GraphRequestError) as caught:
                        graph.get_event(client, 'secret-token', 'secret-id')
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn('secret', str(caught.exception))
        def failure(request):
            raise httpx.ConnectError('certificate verify failed secret-token', request=request)
        with httpx.Client(transport=httpx.MockTransport(failure)) as client:
            with self.assertRaises(graph.GraphRequestError) as caught:
                graph.get_event(client, 'secret-token', 'secret-id')
            self.assertIsNone(caught.exception.status_code)
            self.assertNotIn('secret', str(caught.exception))

    def test_update_without_meeting_and_timezone_conversion(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'id': 'event'})
        zone = timezone(timedelta(hours=2))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertIsNone(graph.update_event(client, 'token', 'event', 'Órbita',
                datetime(2030, 1, 1, 12, tzinfo=zone), datetime(2030, 1, 1, 13, tzinfo=zone)))
        self.assertEqual(json.loads(seen[0].content)['start']['dateTime'], '2030-01-01T10:00:00')

    def test_invalid_retry_after_and_unexpected_delete_success(self):
        for status, retry in ((429, 'secret-token'), (200, '120')):
            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, headers={'Retry-After': retry}))) as client:
                    with self.assertRaises(graph.GraphRequestError) as caught:
                        graph.delete_event(client, 'secret-token', 'event')
                self.assertEqual(caught.exception.status_code, status)
                self.assertIsNone(caught.exception.retry_after)

    def test_dot_segment_id_is_rejected(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('request sent'))) as client:
            for event_id in ('.', '..'):
                with self.assertRaises(ValueError):
                    graph.delete_event(client, 'token', event_id)

    def test_delete_event_encodes_identifier_and_requires_204(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(204)
        with httpx.Client(transport=httpx.MockTransport(handler), timeout=2) as client:
            self.assertIsNone(graph.delete_event(client, 'secret', 'a/b?#'))
        self.assertEqual(seen[0].method, 'DELETE')
        self.assertEqual(seen[0].url.raw_path, b'/v1.0/me/events/a%2Fb%3F%23')
        self.assertEqual(seen[0].content, b'')

    def test_update_and_delete_errors_are_sanitized_and_expose_status(self):
        for method in ('update', 'delete'):
            for status in (404, 429, 500):
                with self.subTest(method=method, status=status):
                    def handler(request):
                        return httpx.Response(status, text='secret response', headers={'Retry-After': '120'})
                    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                        with self.assertRaises(graph.GraphRequestError) as caught:
                            if method == 'update':
                                graph.update_event(client, 'secret-token', 'secret-id', 'secret subject',
                                    datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                                    datetime(2030, 1, 1, 11, tzinfo=timezone.utc))
                            else:
                                graph.delete_event(client, 'secret-token', 'secret-id')
                    self.assertEqual(caught.exception.status_code, status)
                    self.assertEqual(caught.exception.retry_after, '120' if status == 429 else None)
                    self.assertNotIn('secret', str(caught.exception))

    def test_timeout_is_sanitized(self):
        def handler(request):
            raise httpx.ReadTimeout('secret-token secret-id', request=request)
        with httpx.Client(transport=httpx.MockTransport(handler), timeout=1) as client:
            with self.assertRaises(graph.GraphRequestError) as caught:
                graph.delete_event(client, 'secret-token', 'secret-id')
        self.assertIsNone(caught.exception.status_code)
        self.assertNotIn('secret', str(caught.exception))

    def test_update_event_sends_only_editable_fields(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/join'}})
        with httpx.Client(transport=httpx.MockTransport(handler), timeout=3) as client:
            result = graph.update_event(client, 'secret', 'id/with?#', 'Química',
                                        datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                                        datetime(2030, 1, 1, 11, tzinfo=timezone.utc))
        self.assertEqual(result, 'https://teams.microsoft.com/join')
        self.assertEqual(requests[0].method, 'PATCH')
        self.assertEqual(requests[0].url.raw_path, b'/v1.0/me/events/id%2Fwith%3F%23')
        self.assertEqual(json.loads(requests[0].content), {'subject': 'Química',
            'start': {'dateTime': '2030-01-01T10:00:00', 'timeZone': 'UTC'},
            'end': {'dateTime': '2030-01-01T11:00:00', 'timeZone': 'UTC'}})

    def test_create_teams_event_without_attendees(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(201, json={'id': 'event-id', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = create_event(client, 'test-access-token', 'Alumno_2030-01-01_student@example.test',
                                  datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                                  datetime(2030, 1, 1, 11, tzinfo=timezone.utc), 'stable-id')
        self.assertEqual(result, ('event-id', 'https://teams.microsoft.com/meeting'))
        request = requests[0]
        self.assertEqual(str(request.url), 'https://graph.microsoft.com/v1.0/me/events')
        payload = json.loads(request.content)
        self.assertEqual(payload['transactionId'], 'stable-id')
        self.assertTrue(payload['isOnlineMeeting'])
        self.assertEqual(payload['onlineMeetingProvider'], 'teamsForBusiness')
        self.assertNotIn('attendees', payload)
        self.assertEqual(request.headers['authorization'], 'Bearer test-access-token')


if __name__ == '__main__':
    unittest.main()
