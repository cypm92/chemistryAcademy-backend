import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import microsoft_oauth_routes as oauth
from app.main import app
from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration
from app.models import Booking
from app.teams_models import TeamsSyncJob
from test_teams_api import context


@pytest.fixture
def review(context, monkeypatch):
    sessions, client, (admin, _student, _), bid = context
    key = Fernet.generate_key().decode()
    tenant = '11111111-1111-4111-8111-111111111111'
    account = '33333333-3333-4333-8333-333333333333'
    config = oauth.Config(tenant, '22222222-2222-4222-8222-222222222222', 'fake', 'https://example.test/integrations/microsoft/callback', key)
    monkeypatch.setattr(oauth, 'get_config', lambda: config)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        booking = db.get(Booking, bid)
        job.starts_at, job.ends_at, job.last_error = booking.starts_at, booking.ends_at, 'creation_result_unknown'
        db.add(MicrosoftOAuthGeneration(id=1, generation=3, tenant_id=tenant, account_object_id=account))
        db.add(MicrosoftCredential(id=1, account_object_id=account, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'fake-refresh').decode()))
    state = {'on_graph': lambda: pytest.fail('Unexpected Graph request')}
    def handler(request):
        state['on_graph']()
        raise AssertionError('Unexpected network request')
    app.dependency_overrides[oauth.get_session_factory] = lambda: sessions
    app.dependency_overrides[oauth.get_http_transport] = lambda: httpx.MockTransport(handler)
    return sessions, client, admin, bid, key, state, None
