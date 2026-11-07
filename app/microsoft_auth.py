"""Incomplete OAuth building blocks; not an active integration.

The caller must verify that admin_id identifies an active administrator both
when creating and after consuming state. The browser nonce must be supplied
only through a secure HttpOnly SameSite=Lax cookie. No endpoints are provided.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from .microsoft_auth_models import MicrosoftOAuthState, MicrosoftOAuthGeneration


def lock_generation(db):
    dialect = db.bind.dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('Unsupported database for Microsoft OAuth')
    db.execute(insert(MicrosoftOAuthGeneration).values(id=1, generation=0).on_conflict_do_nothing(index_elements=['id']))
    return db.execute(select(MicrosoftOAuthGeneration).where(MicrosoftOAuthGeneration.id == 1).with_for_update()).scalar_one()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def create_oauth_state(session_factory, key: str, admin_id: int, verifier: str):
    cipher = Fernet(key.encode('ascii'))
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with session_factory.begin() as db:
        generation = lock_generation(db).generation
        db.add(MicrosoftOAuthState(
            state_hash=_digest(state), browser_hash=_digest(browser), admin_id=admin_id,
            verifier_ciphertext=cipher.encrypt(verifier.encode()).decode('ascii'),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            generation=generation,
        ))
    return state, browser


def consume_oauth_state(session_factory, key: str, state: str, browser: str):
    cipher = Fernet(key.encode('ascii'))
    if not state or not browser or len(state) > 256 or len(browser) > 256:
        return None
    with session_factory.begin() as db:
        row = db.execute(
            delete(MicrosoftOAuthState).where(
                MicrosoftOAuthState.state_hash == _digest(state),
                MicrosoftOAuthState.browser_hash == _digest(browser),
                MicrosoftOAuthState.expires_at > datetime.now(timezone.utc),
            ).returning(MicrosoftOAuthState.admin_id, MicrosoftOAuthState.verifier_ciphertext, MicrosoftOAuthState.generation)
        ).first()
        if row is None:
            return None
        admin_id, ciphertext, generation = row
        verifier = cipher.decrypt(ciphertext.encode('ascii')).decode('utf-8')
    return admin_id, verifier, generation


def save_refresh_token(session_factory, key: str, refresh_token: str):
    """Store encrypted data only; not a concurrent token-rotation protocol."""
    from .microsoft_auth_models import MicrosoftCredential
    ciphertext = Fernet(key.encode('ascii')).encrypt(refresh_token.encode()).decode('ascii')
    with session_factory.begin() as db:
        credential = db.get(MicrosoftCredential, 1)
        if credential is None:
            db.add(MicrosoftCredential(id=1, refresh_token_ciphertext=ciphertext))
        else:
            credential.refresh_token_ciphertext = ciphertext


def read_refresh_token(session_factory, key: str):
    """Internal primitive: never expose its result through an HTTP response."""
    from .microsoft_auth_models import MicrosoftCredential
    with session_factory() as db:
        credential = db.get(MicrosoftCredential, 1)
        if credential is None:
            return None
        return Fernet(key.encode('ascii')).decrypt(
            credential.refresh_token_ciphertext.encode('ascii')
        ).decode('utf-8')
