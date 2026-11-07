import unittest
import httpx
from transport import SandboxTransport


class SandboxTransportTests(unittest.TestCase):
    def test_only_known_https_hosts_are_forwarded_to_fake(self):
        calls = []
        def fake(request):
            calls.append(request)
            return httpx.Response(200, json={'fake': True})
        with httpx.Client(transport=SandboxTransport('http://fake-microsoft:8080', httpx.MockTransport(fake))) as client:
            response = client.get('https://graph.microsoft.com/v1.0/me')
            self.assertEqual(response.json(), {'fake': True})
            self.assertEqual(str(calls[0].url), 'http://fake-microsoft:8080/v1.0/me')
            with self.assertRaises(httpx.UnsupportedProtocol):
                client.get('https://example.org/leak')
            self.assertEqual(len(calls), 1)

    def test_rejects_host_confusion_and_nonlocal_upstream(self):
        with self.assertRaises(ValueError):
            SandboxTransport('https://graph.microsoft.com')
        t = SandboxTransport('http://fake-microsoft:8080', httpx.MockTransport(lambda r: httpx.Response(200)))
        with httpx.Client(transport=t) as client:
            for url in ('http://graph.microsoft.com/v1.0/me', 'https://graph.microsoft.com:444/v1.0/me', 'https://graph.microsoft.com.evil.test/v1.0/me'):
                with self.assertRaises(httpx.UnsupportedProtocol):
                    client.get(url)

if __name__ == '__main__':
    unittest.main()
