import httpx
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from cryptography.fernet import Fernet
from ephemeral_postgres import postgres_test_db
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.microsoft_auth_models import MicrosoftCredential
from app.microsoft_oauth_routes import Config, SCOPES


@pytest.fixture
def setup_refresh(postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'old-token').decode()))
    yield factory, config
    engine.dispose()


def test_refresh_rotates_encrypted_token(postgres_test_db):
    from app.microsoft_refresh import get_access_token

    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'old-token').decode()))

    def handler(request):
        from urllib.parse import parse_qs
        body = parse_qs(request.content.decode())
        assert request.url.host == 'login.microsoftonline.com'
        assert request.url.path == f'/{config.tenant}/oauth2/v2.0/token'
        assert body['refresh_token'] == ['old-token']
        assert body['scope'] == [SCOPES]
        return httpx.Response(200, json={'access_token': 'access-secret', 'refresh_token': 'new-token'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_access_token(factory, config, client) == 'access-secret'
    with factory() as db:
        row = db.get(MicrosoftCredential, 1)
        assert 'new-token' not in row.refresh_token_ciphertext
        assert Fernet(key.encode()).decrypt(row.refresh_token_ciphertext.encode()) == b'new-token'


def test_restarted_sandbox_marks_credential_for_reconnection(setup_refresh):
    import importlib.util
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app.microsoft_refresh import get_access_token, MicrosoftReconnectRequired

    path = Path(__file__).resolve().parents[1] / 'local-microsoft-sandbox' / 'server.py'
    spec = importlib.util.spec_from_file_location('local_microsoft_sandbox_server', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory, config = setup_refresh
    # A fresh app has forgotten the refresh token stored by the backend.
    with TestClient(module.create_app()) as client:
        with pytest.raises(MicrosoftReconnectRequired):
            get_access_token(factory, config, client)
    with factory() as db:
        row = db.get(MicrosoftCredential, 1)
        assert row.needs_reconnect
        assert row.refresh_lease_id is None


def test_revoked_marks_reconnect_without_leaking_response(setup_refresh):
    from app.microsoft_refresh import get_access_token, MicrosoftReconnectRequired
    factory, config = setup_refresh
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={'error': 'invalid_grant', 'error_description': 'sensitive-token'}))) as client:
        with pytest.raises(MicrosoftReconnectRequired) as exc:
            get_access_token(factory, config, client)
        assert 'sensitive-token' not in str(exc.value)
        with pytest.raises(MicrosoftReconnectRequired):
            get_access_token(factory, config, client)
    with factory() as db:
        assert db.get(MicrosoftCredential, 1).needs_reconnect


@pytest.mark.parametrize('response', [httpx.Response(503), httpx.Response(200, json={'access_token': 'secret'}), httpx.Response(200, text='invalid')])
def test_transient_and_malformed_responses_preserve_credential(setup_refresh, response):
    from app.microsoft_refresh import get_access_token, MicrosoftRefreshError
    factory, config = setup_refresh
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(MicrosoftRefreshError):
            get_access_token(factory, config, client)
    with factory() as db:
        row = db.get(MicrosoftCredential, 1)
        assert not row.needs_reconnect
        assert Fernet(config.key.encode()).decrypt(row.refresh_token_ciphertext.encode()) == b'old-token'


def test_timeout_and_redirects_are_explicit_and_error_is_sanitized(setup_refresh):
    from app.microsoft_refresh import get_access_token, MicrosoftRefreshError
    factory, config = setup_refresh
    def handler(request):
        assert request.extensions['timeout'] == {'connect': 5.0, 'read': 5.0, 'write': 5.0, 'pool': 5.0}
        raise httpx.ReadTimeout('old-token secret', request=request)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(MicrosoftRefreshError) as exc:
            get_access_token(factory, config, client)
        assert 'old-token' not in str(exc.value)


def test_postgres_refresh_serializes_and_disconnect_wins(postgres_test_db):
    from sqlalchemy import delete
    from app.microsoft_refresh import get_access_token
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.execute(delete(MicrosoftCredential))
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'old').decode()))
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    def handler(request):
        entered.set()
        assert release.wait(4)
        return httpx.Response(200, json={'access_token': 'access', 'refresh_token': 'new'})
    def refresh():
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return get_access_token(factory, config, client)
    def disconnect():
        with factory.begin() as db:
            deleting.set()
            db.execute(delete(MicrosoftCredential))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(refresh)
            assert entered.wait(4)
            two = pool.submit(disconnect)
            assert deleting.wait(4)
            two.result(timeout=3)  # Disconnect must not wait for Microsoft.
            release.set()
            from app.microsoft_refresh import MicrosoftRefreshError
            with pytest.raises(MicrosoftRefreshError):
                one.result(timeout=5)
        with factory() as db:
            assert db.get(MicrosoftCredential, 1) is None
    finally:
        release.set()
        with factory.begin() as db:
            db.execute(delete(MicrosoftCredential))
        engine.dispose()


def test_postgres_two_refreshes_consume_rotated_token_in_order(postgres_test_db):
    from urllib.parse import parse_qs
    from sqlalchemy import delete
    from app.microsoft_refresh import get_access_token
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.execute(delete(MicrosoftCredential))
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'old').decode()))
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def handler(request):
        token = parse_qs(request.content.decode())['refresh_token'][0]
        seen.append(token)
        if token == 'old':
            entered.set()
            assert release.wait(4)
        return httpx.Response(200, json={'access_token': 'access', 'refresh_token': 'new' if token == 'old' else 'newer'})
    def refresh(started=None):
        if started:
            started.set()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return get_access_token(factory, config, client)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refresh)
            assert entered.wait(4)
            second = pool.submit(refresh, second_started)
            assert second_started.wait(4)
            release.set()
            assert first.result(timeout=5) == second.result(timeout=5) == 'access'
        assert seen == ['old', 'new']
        with factory() as db:
            assert Fernet(key.encode()).decrypt(db.get(MicrosoftCredential, 1).refresh_token_ciphertext.encode()) == b'newer'
    finally:
        release.set()
        with factory.begin() as db:
            db.execute(delete(MicrosoftCredential))
        engine.dispose()


@pytest.mark.parametrize('stale_response', [(200, {'access_token': 'wrong-account', 'refresh_token': 'stale'}), (400, {'error': 'invalid_grant'})])
def test_postgres_reconnect_wins_over_inflight_refresh(stale_response, postgres_test_db):
    from sqlalchemy import delete
    from app.microsoft_refresh import get_access_token, MicrosoftRefreshError
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    cipher = Fernet(key.encode())
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.execute(delete(MicrosoftCredential))
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=cipher.encrypt(b'old').decode()))
    entered, release = threading.Event(), threading.Event()
    def handler(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(stale_response[0], json=stale_response[1])
    def refresh():
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return get_access_token(factory, config, client)
    def reconnect():
        with factory.begin() as db:
            row = db.get(MicrosoftCredential, 1)
            row.refresh_token_ciphertext = cipher.encrypt(b'reconnected').decode()
            row.needs_reconnect = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refresh)
            assert entered.wait(4)
            pool.submit(reconnect).result(timeout=3)
            release.set()
            with pytest.raises(MicrosoftRefreshError):
                first.result(timeout=5)
        with factory() as db:
            row = db.get(MicrosoftCredential, 1)
            assert cipher.decrypt(row.refresh_token_ciphertext.encode()) == b'reconnected'
            assert not row.needs_reconnect
    finally:
        release.set()
        with factory.begin() as db:
            db.execute(delete(MicrosoftCredential))
        engine.dispose()


def test_postgres_expired_lease_fences_stale_worker(postgres_test_db):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from app.microsoft_refresh import get_access_token, MicrosoftRefreshError
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    cipher = Fernet(key.encode())
    config = Config('11111111-1111-4111-8111-111111111111', 'client', 'secret', 'https://example.test/integrations/microsoft/callback', key)
    with factory.begin() as db:
        db.execute(delete(MicrosoftCredential))
        db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=cipher.encrypt(b'old').decode()))
    entered, release = threading.Event(), threading.Event()
    def first_handler(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(400, json={'error': 'invalid_grant'})
    def refresh(handler):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return get_access_token(factory, config, client)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refresh, first_handler)
            assert entered.wait(4)
            with factory.begin() as db:
                db.get(MicrosoftCredential, 1).refresh_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            second = pool.submit(refresh, lambda request: httpx.Response(200, json={'access_token': 'new-access', 'refresh_token': 'rotated'}))
            assert second.result(timeout=3) == 'new-access'
            release.set()
            with pytest.raises(MicrosoftRefreshError):
                first.result(timeout=5)
        with factory() as db:
            row = db.get(MicrosoftCredential, 1)
            assert cipher.decrypt(row.refresh_token_ciphertext.encode()) == b'rotated'
            assert not row.needs_reconnect
    finally:
        release.set()
        with factory.begin() as db:
            db.execute(delete(MicrosoftCredential))
        engine.dispose()
