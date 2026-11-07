import os
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration
from app.microsoft_oauth_routes import Config
from app import microsoft_refresh

TENANT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
ACCOUNT = '22222222-2222-4222-8222-222222222222'


@pytest.fixture
def identity_db(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'identity.db'))
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__, MicrosoftOAuthGeneration.__table__])
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    config = Config(TENANT.upper(), 'client', 'secret', 'https://example.test/callback', key)
    with factory.begin() as db:
        db.add(MicrosoftCredential(id=1, account_object_id=ACCOUNT, refresh_token_ciphertext=Fernet(key.encode()).encrypt(b'old').decode()))
        db.add(MicrosoftOAuthGeneration(id=1, generation=7, account_object_id=ACCOUNT, tenant_id=TENANT))
    yield factory, config
    engine.dispose()


def test_returns_fenced_identity_and_hides_token_in_repr(identity_db):
    factory, config = identity_db
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'access_token': 'private-access', 'refresh_token': 'rotated'}))) as client:
        result = microsoft_refresh.get_access_token_with_identity(factory, config, client)
    assert (result.token, result.tenant_id, result.account_object_id, result.oauth_generation) == ('private-access', TENANT, ACCOUNT, 7)
    assert 'private-access' not in repr(result)


@pytest.mark.parametrize('change', ['missing_binding', 'missing_account', 'wrong_account', 'wrong_tenant'])
def test_missing_or_mismatched_identity_fails_before_http(identity_db, change):
    factory, config = identity_db
    with factory.begin() as db:
        binding = db.get(MicrosoftOAuthGeneration, 1)
        if change == 'missing_binding':
            db.delete(binding)
        elif change == 'missing_account':
            binding.account_object_id = None
        elif change == 'wrong_account':
            db.get(MicrosoftCredential, 1).account_object_id = 'other'
        else:
            binding.tenant_id = 'other'
    def no_http(request):
        pytest.fail('HTTP must not occur for an unbound credential')
    with httpx.Client(transport=httpx.MockTransport(no_http)) as client:
        with pytest.raises(microsoft_refresh.MicrosoftReconnectRequired):
            microsoft_refresh.get_access_token_with_identity(factory, config, client)


@pytest.mark.skipif(not os.getenv('ISOLATED_POSTGRES_TEST_URL'), reason='requires disposable PostgreSQL')
@pytest.mark.parametrize('change', ['generation', 'binding_account', 'credential_account', 'reconnect', 'disconnect'])
def test_postgres_identity_change_during_http_fences_result(change):
    engine = create_engine(os.environ['ISOLATED_POSTGRES_TEST_URL'])
    tables = [MicrosoftCredential.__table__, MicrosoftOAuthGeneration.__table__]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine)
    key = Fernet.generate_key().decode()
    cipher = Fernet(key.encode())
    config = Config(TENANT, 'client', 'secret', 'https://example.test/callback', key)
    with factory.begin() as db:
        db.add(MicrosoftCredential(id=1, account_object_id=ACCOUNT, refresh_token_ciphertext=cipher.encrypt(b'old').decode()))
        db.add(MicrosoftOAuthGeneration(id=1, generation=7, account_object_id=ACCOUNT, tenant_id=TENANT))
    entered, release = threading.Event(), threading.Event()
    def handler(request):
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json={'access_token': 'stale-secret', 'refresh_token': 'stale-rotation'})
    def refresh():
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return microsoft_refresh.get_access_token_with_identity(factory, config, client)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(refresh)
            assert entered.wait(10)
            with factory.begin() as db:
                binding = db.get(MicrosoftOAuthGeneration, 1)
                credential = db.get(MicrosoftCredential, 1)
                if change == 'generation':
                    binding.generation += 1
                elif change == 'binding_account':
                    binding.account_object_id = 'other'
                elif change == 'credential_account':
                    credential.account_object_id = 'other'
                elif change == 'reconnect':
                    binding.generation += 1
                    binding.account_object_id = 'other'
                    credential.account_object_id = 'other'
                    credential.refresh_token_ciphertext = cipher.encrypt(b'reconnected').decode()
                else:
                    binding.generation += 1
                    db.delete(credential)
            release.set()
            with pytest.raises(microsoft_refresh.MicrosoftRefreshError) as exc:
                future.result(timeout=10)
            assert 'stale-secret' not in str(exc.value)
        with factory() as db:
            credential = db.get(MicrosoftCredential, 1)
            if change == 'reconnect':
                assert cipher.decrypt(credential.refresh_token_ciphertext.encode()) == b'reconnected'
            elif change != 'disconnect':
                assert cipher.decrypt(credential.refresh_token_ciphertext.encode()) == b'old'
            else:
                assert credential is None
    finally:
        release.set()
        with factory.begin() as db:
            db.query(MicrosoftCredential).delete()
            db.query(MicrosoftOAuthGeneration).delete()
        engine.dispose()
