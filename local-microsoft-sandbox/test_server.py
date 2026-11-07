import unittest
from fastapi.testclient import TestClient
from server import create_app


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_identity_requires_fake_token(self):
        response = self.client.get('/v1.0/me')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'InvalidAuthenticationToken')
        self.assertEqual(self.client.get('/unknown').status_code, 404)

    def test_token_errors_use_oauth_format(self):
        for form, expected in [
            ({'grant_type': 'refresh_token', 'refresh_token': 'old-token'}, 'invalid_grant'),
            ({'grant_type': 'authorization_code', 'code': 'old-code'}, 'invalid_grant'),
            ({'grant_type': 'unknown'}, 'unsupported_grant_type'),
        ]:
            with self.subTest(form=form):
                response = self.client.post('/tenant/oauth2/v2.0/token', data=form)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], expected)


if __name__ == '__main__':
    unittest.main()
