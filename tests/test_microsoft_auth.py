import pytest
from sqlalchemy.orm import sessionmaker
from ephemeral_postgres import postgres_test_db


def test_state_is_encrypted_bound_to_browser_and_single_use(postgres_test_db):
    from cryptography.fernet import Fernet
    from app.database import Base
    from app.microsoft_auth import create_oauth_state, consume_oauth_state
    from app.microsoft_auth_models import MicrosoftOAuthState, MicrosoftOAuthGeneration

    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftOAuthState.__table__, MicrosoftOAuthGeneration.__table__])
    factory = sessionmaker(bind=engine)
    key = Fernet.generate_key().decode()
    state, browser = create_oauth_state(factory, key, admin_id=12, verifier='secret-verifier')
    with factory() as db:
        row = db.query(MicrosoftOAuthState).one()
        assert state not in row.state_hash
        assert browser not in row.browser_hash
        assert 'secret-verifier' not in row.verifier_ciphertext
    assert consume_oauth_state(factory, key, state, 'wrong-browser') is None
    assert consume_oauth_state(factory, key, state, browser) == (12, 'secret-verifier', 0)
    assert consume_oauth_state(factory, key, state, browser) is None


def test_expired_state_is_rejected(postgres_test_db):
    from datetime import datetime, timedelta, timezone
    from cryptography.fernet import Fernet
    from app.database import Base
    from app.microsoft_auth import create_oauth_state, consume_oauth_state
    from app.microsoft_auth_models import MicrosoftOAuthState, MicrosoftOAuthGeneration
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftOAuthState.__table__, MicrosoftOAuthGeneration.__table__])
    factory = sessionmaker(bind=engine)
    key = Fernet.generate_key().decode()
    state, browser = create_oauth_state(factory, key, 12, 'verifier')
    with factory.begin() as db:
        db.query(MicrosoftOAuthState).one().expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert consume_oauth_state(factory, key, state, browser) is None


def test_refresh_token_storage_is_encrypted_and_decryptable(postgres_test_db):
    from cryptography.fernet import Fernet
    from app.database import Base
    from app.microsoft_auth import save_refresh_token, read_refresh_token
    from app.microsoft_auth_models import MicrosoftCredential
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine, tables=[MicrosoftCredential.__table__])
    factory = sessionmaker(bind=engine)
    key = Fernet.generate_key().decode()
    save_refresh_token(factory, key, 'fictional-refresh-token')
    with factory() as db:
        row = db.query(MicrosoftCredential).one()
        assert 'fictional-refresh-token' not in row.refresh_token_ciphertext
    assert read_refresh_token(factory, key) == 'fictional-refresh-token'
