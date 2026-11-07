from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_status_disabled_without_configuration(monkeypatch):
    from app.microsoft_oauth_routes import router
    from app.security import admin_user
    for name in ('MICROSOFT_TENANT_ID', 'MICROSOFT_CLIENT_ID', 'MICROSOFT_CLIENT_SECRET', 'MICROSOFT_REDIRECT_URI', 'MICROSOFT_FERNET_KEY'):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[admin_user] = lambda: object()
    response = TestClient(app).get('/admin/integrations/microsoft')
    assert response.status_code == 200
    assert response.json() == {'configured': False, 'connected': False, 'account_label': None, 'needs_reconnect': False}


def test_public_proxy_callback_config_and_cookie(monkeypatch):
    from cryptography.fernet import Fernet
    from app.microsoft_oauth_routes import get_config
    for key, value in {'MICROSOFT_TENANT_ID': '11111111-1111-4111-8111-111111111111', 'MICROSOFT_CLIENT_ID': '22222222-2222-4222-8222-222222222222', 'MICROSOFT_CLIENT_SECRET': 'fake', 'MICROSOFT_REDIRECT_URI': 'https://example.test/api/integrations/microsoft/callback', 'MICROSOFT_FERNET_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key, value)
    assert get_config() is not None
    from app import microsoft_oauth_routes as routes
    from app.security import admin_user
    monkeypatch.setattr(routes, 'create_oauth_state', lambda *args: ('fake-state', 'fake-browser'))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[admin_user] = lambda: type('Admin', (), {'id': 1})()
    started = TestClient(app, base_url='https://example.test').post('/admin/integrations/microsoft/start')
    assert 'Path=/api/integrations/microsoft/callback' in started.headers['set-cookie']
    assert 'Path=/api/integrations/microsoft/callback' in routes.finish(path='/api/integrations/microsoft/callback').headers['set-cookie']


def test_calendar_scope_is_requested():
    from app.microsoft_oauth_routes import SCOPES
    assert 'Calendars.ReadWrite' in SCOPES.split()


def test_flow_and_replay(monkeypatch):
    import hashlib, base64
    from urllib.parse import urlparse, parse_qs
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    from app.models import User
    from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthState, MicrosoftOAuthGeneration
    from app.microsoft_oauth_routes import router, get_session_factory, get_http_transport
    from app.security import admin_user
    tenant = '11111111-1111-4111-8111-111111111111'
    client_id = '22222222-2222-4222-8222-222222222222'
    for key, val in {'MICROSOFT_TENANT_ID': tenant, 'MICROSOFT_CLIENT_ID': client_id, 'MICROSOFT_CLIENT_SECRET': 'fake-secret', 'MICROSOFT_REDIRECT_URI': 'https://example.test/integrations/microsoft/callback', 'MICROSOFT_FERNET_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key, val)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, MicrosoftCredential.__table__, MicrosoftOAuthState.__table__, MicrosoftOAuthGeneration.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        admin = User(name='Admin', email='admin@example.test', password_hash='x', role='admin', is_active=True)
        db.add(admin)
        db.flush()
        admin_id = admin.id
    calls = []
    identity = ['33333333-3333-4333-8333-333333333333']
    def handler(request):
        calls.append(request)
        if request.url.host == 'login.microsoftonline.com':
            assert request.url.path == f"/{__import__('os').environ['MICROSOFT_TENANT_ID']}/oauth2/v2.0/token"
            return httpx.Response(200, json={'access_token': 'fake-access', 'refresh_token': 'fake-refresh'})
        assert request.url.host == 'graph.microsoft.com'
        return httpx.Response(200, json={'id': identity[0], 'displayName': 'Test Account'})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[admin_user] = lambda: admin
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_http_transport] = lambda: httpx.MockTransport(handler)
    browser = TestClient(app, base_url='https://example.test', follow_redirects=False)
    started = browser.post('/admin/integrations/microsoft/start')
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()['authorization_url']).query)
    assert query['code_challenge_method'] == ['S256']
    assert 'HttpOnly' in started.headers['set-cookie'] and 'Secure' in started.headers['set-cookie']
    state = query['state'][0]
    other = TestClient(app, base_url='https://example.test', follow_redirects=False)
    assert other.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'}).status_code == 303
    assert not calls
    simultaneous = other.post('/admin/integrations/microsoft/start')
    other_state = parse_qs(urlparse(simultaneous.json()['authorization_url']).query)['state'][0]
    result = browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'})
    stale = other.get('/integrations/microsoft/callback', params={'state': other_state, 'code': 'fake-code'})
    assert stale.headers['location'] == '/admin?microsoft=error'
    calls[:] = calls[:2]
    assert result.status_code == 303 and result.headers['location'] == '/admin?microsoft=connected'
    token_form = parse_qs(calls[0].content.decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(token_form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
    assert challenge == query['code_challenge'][0]
    with factory() as db:
        row = db.get(MicrosoftCredential, 1)
        assert 'fake-refresh' not in row.refresh_token_ciphertext
        assert row.account_label == 'Test Account'
    assert browser.get('/admin/integrations/microsoft').json()['connected'] is True
    browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'})
    assert len(calls) == 2
    assert browser.post('/admin/integrations/microsoft/disconnect').status_code == 200
    assert browser.get('/admin/integrations/microsoft').json()['connected'] is False
    identity[0] = '44444444-4444-4444-8444-444444444444'
    state = parse_qs(urlparse(browser.post('/admin/integrations/microsoft/start').json()['authorization_url']).query)['state'][0]
    rejected = browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'})
    assert rejected.headers['location'] == '/admin?microsoft=error'
    with factory() as db:
        assert db.get(MicrosoftCredential, 1) is None
    identity[0] = '33333333-3333-4333-8333-333333333333'
    monkeypatch.setenv('MICROSOFT_TENANT_ID', '55555555-5555-4555-8555-555555555555')
    state = parse_qs(urlparse(browser.post('/admin/integrations/microsoft/start').json()['authorization_url']).query)['state'][0]
    assert browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'}).headers['location'] == '/admin?microsoft=error'
    monkeypatch.setenv('MICROSOFT_TENANT_ID', tenant)
    state = parse_qs(urlparse(browser.post('/admin/integrations/microsoft/start').json()['authorization_url']).query)['state'][0]
    assert browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'}).headers['location'] == '/admin?microsoft=connected'
    assert browser.post('/admin/integrations/microsoft/disconnect').status_code == 200
    calls.clear()

    state = parse_qs(urlparse(browser.post('/admin/integrations/microsoft/start').json()['authorization_url']).query)['state'][0]
    cancelled = browser.get('/integrations/microsoft/callback', params={'state': state, 'error': 'access_denied', 'error_description': 'secret details'})
    assert cancelled.headers['location'] == '/admin?microsoft=error'
    assert 'secret details' not in str(cancelled.headers)
    assert len(calls) == 0
    state = parse_qs(urlparse(browser.post('/admin/integrations/microsoft/start').json()['authorization_url']).query)['state'][0]
    with factory.begin() as db:
        db.get(User, admin_id).is_active = False
    browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake-code'})
    assert len(calls) == 0


def test_invalid_configuration_is_disabled(monkeypatch):
    from cryptography.fernet import Fernet
    from app.microsoft_oauth_routes import get_config
    values = {'MICROSOFT_TENANT_ID': '11111111-1111-4111-8111-111111111111', 'MICROSOFT_CLIENT_ID': '22222222-2222-4222-8222-222222222222', 'MICROSOFT_CLIENT_SECRET': 'fake', 'MICROSOFT_REDIRECT_URI': 'https://example.test/integrations/microsoft/callback', 'MICROSOFT_FERNET_KEY': Fernet.generate_key().decode()}
    for key, value in values.items(): monkeypatch.setenv(key, value)
    assert get_config() is not None
    for key, invalid in [('MICROSOFT_TENANT_ID', 'common'), ('MICROSOFT_CLIENT_ID', 'bad'), ('MICROSOFT_FERNET_KEY', 'invalid'), ('MICROSOFT_REDIRECT_URI', 'http://evil.test/integrations/microsoft/callback'), ('MICROSOFT_REDIRECT_URI', 'https://evil.test@localhost/integrations/microsoft/callback'), ('MICROSOFT_REDIRECT_URI', 'https://example.test/integrations/microsoft/callback?x=1')]:
        monkeypatch.setenv(key, invalid)
        assert get_config() is None
        monkeypatch.setenv(key, values[key])
    monkeypatch.setenv('MICROSOFT_REDIRECT_URI', 'http://localhost:8080/integrations/microsoft/callback')
    assert get_config() is not None


def test_admin_endpoints_require_bearer():
    from app.microsoft_oauth_routes import router
    app = FastAPI()
    app.include_router(router)
    browser = TestClient(app)
    assert browser.get('/admin/integrations/microsoft').status_code == 401
    assert browser.post('/admin/integrations/microsoft/start').status_code == 401
    assert browser.post('/admin/integrations/microsoft/disconnect').status_code == 401


def test_disconnect_during_callback_prevents_reconnection(monkeypatch, tmp_path):
    from urllib.parse import urlparse, parse_qs
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import User
    from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthState, MicrosoftOAuthGeneration
    from app.microsoft_oauth_routes import router, get_session_factory, get_http_transport
    from app.security import admin_user
    for key, value in {'MICROSOFT_TENANT_ID':'11111111-1111-4111-8111-111111111111', 'MICROSOFT_CLIENT_ID':'22222222-2222-4222-8222-222222222222', 'MICROSOFT_CLIENT_SECRET':'fake', 'MICROSOFT_REDIRECT_URI':'https://example.test/integrations/microsoft/callback', 'MICROSOFT_FERNET_KEY':Fernet.generate_key().decode()}.items(): monkeypatch.setenv(key, value)
    import os
    url = os.getenv('OAUTH_ISOLATED_POSTGRES_TEST_URL')
    engine = create_engine(url) if url else create_engine('sqlite:///' + str(tmp_path / 'test.db'), connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        admin = User(name='Admin', email='admin@example.test', password_hash='x', role='admin', is_active=True)
        db.add(admin)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[admin_user] = lambda: admin
    app.dependency_overrides[get_session_factory] = lambda: factory
    browser = TestClient(app, base_url='https://example.test', follow_redirects=False)
    def handler(request):
        if request.url.host == 'login.microsoftonline.com':
            assert browser.post('/admin/integrations/microsoft/disconnect').status_code == 200
            return httpx.Response(200, json={'access_token':'fake', 'refresh_token':'fake'})
        return httpx.Response(200, json={'id':'33333333-3333-4333-8333-333333333333'})
    app.dependency_overrides[get_http_transport] = lambda: httpx.MockTransport(handler)
    started = browser.post('/admin/integrations/microsoft/start')
    state = parse_qs(urlparse(started.json()['authorization_url']).query)['state'][0]
    result = browser.get('/integrations/microsoft/callback', params={'state':state, 'code':'fake'})
    assert result.headers['location'] == '/admin?microsoft=error'
    with factory() as db:
        assert db.get(MicrosoftCredential, 1) is None


def test_postgres_callback_fences_inflight_refresh(monkeypatch):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import parse_qs, urlparse
    from uuid import uuid4

    import httpx
    import pytest
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import User
    from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration, MicrosoftOAuthState
    from app.microsoft_oauth_routes import router, get_session_factory, get_http_transport, get_config
    from app.microsoft_refresh import get_access_token, MicrosoftRefreshError
    from app.security import admin_user

    url = os.getenv('OAUTH_ISOLATED_POSTGRES_TEST_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    key = Fernet.generate_key().decode()
    for name, value in {'MICROSOFT_TENANT_ID': '11111111-1111-4111-8111-111111111111', 'MICROSOFT_CLIENT_ID': '22222222-2222-4222-8222-222222222222', 'MICROSOFT_CLIENT_SECRET': 'fake', 'MICROSOFT_REDIRECT_URI': 'https://example.test/integrations/microsoft/callback', 'MICROSOFT_FERNET_KEY': key}.items():
        monkeypatch.setenv(name, value)
    root = create_engine(url)
    assert root.dialect.name == 'postgresql'
    schema = 'oauth_test_' + uuid4().hex
    with root.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    release = threading.Event()
    try:
        Base.metadata.create_all(engine, tables=[User.__table__, MicrosoftCredential.__table__, MicrosoftOAuthState.__table__, MicrosoftOAuthGeneration.__table__])
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory.begin() as db:
            admin = User(name='Admin', email='admin@example.test', password_hash='x', role='admin', is_active=True)
            db.add(admin)
        account = '33333333-3333-4333-8333-333333333333'
        def oauth_handler(request):
            if request.url.host == 'login.microsoftonline.com':
                return httpx.Response(200, json={'access_token': 'oauth-access', 'refresh_token': 'oauth-refresh'})
            return httpx.Response(200, json={'id': account, 'displayName': 'Account'})
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[admin_user] = lambda: admin
        app.dependency_overrides[get_session_factory] = lambda: factory
        app.dependency_overrides[get_http_transport] = lambda: httpx.MockTransport(oauth_handler)
        browser = TestClient(app, base_url='https://example.test', follow_redirects=False)
        def begin():
            response = browser.post('/admin/integrations/microsoft/start')
            assert response.status_code == 200
            return parse_qs(urlparse(response.json()['authorization_url']).query)['state'][0]
        def finish(state):
            return browser.get('/integrations/microsoft/callback', params={'state': state, 'code': 'fake'})
        assert finish(begin()).headers['location'] == '/admin?microsoft=connected'
        state = begin()
        entered = threading.Event()
        def refresh_handler(request):
            assert parse_qs(request.content.decode())['refresh_token'] == ['oauth-refresh']
            entered.set()
            assert release.wait(10)
            return httpx.Response(200, json={'access_token': 'stale-access', 'refresh_token': 'stale-refresh'})
        def refresh():
            with httpx.Client(transport=httpx.MockTransport(refresh_handler)) as client:
                return get_access_token(factory, get_config(), client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(refresh)
            try:
                assert entered.wait(5)
                with factory() as db:
                    leased = db.get(MicrosoftCredential, 1)
                    generation = leased.refresh_generation
                    assert leased.refresh_lease_id and leased.refresh_lease_expires_at
                result = pool.submit(finish, state).result(timeout=5)
                assert result.headers['location'] == '/admin?microsoft=connected'
                with factory() as db:
                    row = db.get(MicrosoftCredential, 1)
                    assert row.refresh_generation == generation + 1
                    assert row.refresh_lease_id is None
                    assert row.refresh_lease_expires_at is None
                    assert Fernet(key.encode()).decrypt(row.refresh_token_ciphertext.encode()) == b'oauth-refresh'
                release.set()
                with pytest.raises(MicrosoftRefreshError):
                    future.result(timeout=5)
            finally:
                release.set()
        with factory() as db:
            row = db.get(MicrosoftCredential, 1)
            assert Fernet(key.encode()).decrypt(row.refresh_token_ciphertext.encode()) == b'oauth-refresh'
            assert not row.needs_reconnect
    finally:
        release.set()
        engine.dispose()
        with root.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
