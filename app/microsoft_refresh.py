"""Worker-only Microsoft token refresh; access tokens are never persisted."""
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, text

from .microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration
from .microsoft_oauth_routes import SCOPES


class MicrosoftRefreshError(Exception):
    """A sanitized refresh failure safe to expose to worker error handling."""


class MicrosoftReconnectRequired(MicrosoftRefreshError):
    pass


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _db_now(db):
    return db.scalar(select(func.clock_timestamp() if db.bind.dialect.name == 'postgresql' else func.current_timestamp()))


def _lock_row(db):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
    return db.execute(select(MicrosoftCredential).where(MicrosoftCredential.id == 1).with_for_update()).scalar_one_or_none()


@dataclass(frozen=True)
class MicrosoftAccessTokenIdentity:
    token: str = field(repr=False)
    tenant_id: str
    account_object_id: str
    oauth_generation: int


def _binding(db):
    return db.execute(select(MicrosoftOAuthGeneration).where(
        MicrosoftOAuthGeneration.id == 1).with_for_update()).scalar_one_or_none()


def get_access_token(session_factory, config, client):
    return _refresh(session_factory, config, client, identity=False)


def get_access_token_with_identity(session_factory, config, client):
    return _refresh(session_factory, config, client, identity=True)


def _refresh(session_factory, config, client, identity):
    """Claim a durable lease, perform HTTP without a DB lock, then fence the result."""
    try:
        tenant = str(UUID(config.tenant))
        cipher = Fernet(config.key.encode('ascii'))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise MicrosoftRefreshError('Invalid Microsoft configuration') from None

    try:
        deadline = time.monotonic() + 3
        lease_id = str(uuid4())
        while True:
            claimed = False
            bad_token = False
            with session_factory.begin() as db:
                binding = _binding(db) if identity else None
                row = _lock_row(db)
                if row is None or row.needs_reconnect:
                    raise MicrosoftReconnectRequired('Microsoft reconnection required')
                if identity:
                    if (binding is None or not binding.account_object_id
                            or binding.tenant_id != tenant
                            or row.account_object_id != binding.account_object_id):
                        raise MicrosoftReconnectRequired('Microsoft reconnection required')
                    oauth_generation = binding.generation
                    account_object_id = binding.account_object_id
                now = _db_now(db)
                if not row.refresh_lease_id or not row.refresh_lease_expires_at or _utc(row.refresh_lease_expires_at) <= _utc(now):
                    snapshot = row.refresh_token_ciphertext
                    try:
                        refresh = cipher.decrypt(snapshot.encode('ascii')).decode('utf-8')
                    except (InvalidToken, ValueError, UnicodeError):
                        row.needs_reconnect = True
                        bad_token = True
                    else:
                        row.refresh_generation += 1
                        generation = row.refresh_generation
                        row.refresh_lease_id = lease_id
                        row.refresh_lease_expires_at = now + timedelta(seconds=60)
                        claimed = True
            if bad_token:
                raise MicrosoftReconnectRequired('Microsoft reconnection required')
            if claimed:
                break
            if time.monotonic() >= deadline:
                raise MicrosoftRefreshError('Microsoft token refresh temporarily busy')
            time.sleep(0.05)

        failure = None
        reconnect = False
        access = rotated = None
        try:
            response = client.post(
                f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
                data={'client_id': config.client, 'client_secret': config.secret,
                      'grant_type': 'refresh_token', 'refresh_token': refresh, 'scope': SCOPES},
                timeout=httpx.Timeout(5.0), follow_redirects=False,
            )
            if response.status_code != 200:
                try:
                    error = response.json().get('error')
                except (ValueError, AttributeError):
                    error = None
                reconnect = error in {'invalid_grant', 'interaction_required', 'invalid_client', 'unauthorized_client'}
                failure = MicrosoftReconnectRequired('Microsoft reconnection required') if reconnect else MicrosoftRefreshError('Microsoft token refresh failed')
            else:
                tokens = response.json()
                access, rotated = tokens.get('access_token'), tokens.get('refresh_token')
                if not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated:
                    failure = MicrosoftRefreshError('Invalid Microsoft token response')
        except (httpx.HTTPError, ValueError, AttributeError, TypeError, UnicodeError):
            failure = MicrosoftRefreshError('Microsoft token refresh failed')

        superseded = False
        with session_factory.begin() as db:
            binding = _binding(db) if identity else None
            row = _lock_row(db)
            now = _db_now(db)
            if (row is None or row.needs_reconnect or row.refresh_lease_id != lease_id
                    or row.refresh_generation != generation or row.refresh_token_ciphertext != snapshot
                    or not row.refresh_lease_expires_at
                    or _utc(row.refresh_lease_expires_at) <= _utc(now)
                    or (identity and (binding is None or binding.generation != oauth_generation
                        or binding.tenant_id != tenant or binding.account_object_id != account_object_id
                        or row.account_object_id != account_object_id))):
                superseded = True
                if row is not None and row.refresh_lease_id == lease_id and row.refresh_generation == generation:
                    row.refresh_lease_id = None
                    row.refresh_lease_expires_at = None
            else:
                row.refresh_lease_id = None
                row.refresh_lease_expires_at = None
                if reconnect:
                    row.needs_reconnect = True
                elif failure is None:
                    row.refresh_token_ciphertext = cipher.encrypt(rotated.encode()).decode('ascii')
        if superseded:
            raise MicrosoftRefreshError('Microsoft token refresh superseded')
        if failure:
            raise failure
        if identity:
            return MicrosoftAccessTokenIdentity(access, tenant, account_object_id, oauth_generation)
        return access
    except MicrosoftRefreshError:
        raise
    except Exception:
        # Database and client exceptions may contain credentials or SQL parameters.
        raise MicrosoftRefreshError('Microsoft token refresh unavailable') from None
