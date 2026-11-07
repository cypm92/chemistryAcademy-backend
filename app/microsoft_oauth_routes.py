"""Standalone Microsoft OAuth router; caller is responsible for schema setup."""
import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from .database import SessionLocal
from .microsoft_auth import create_oauth_state, consume_oauth_state, lock_generation
from .microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthState
from .models import User
from .security import admin_user

router = APIRouter()
COOKIE = 'microsoft_oauth_nonce'
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}
SCOPES = 'openid profile offline_access User.Read Calendars.ReadWrite'

@dataclass(frozen=True)
class Config:
    tenant: str
    client: str
    secret: str
    redirect: str
    key: str

def get_config():
    try:
        tenant = str(UUID(os.environ['MICROSOFT_TENANT_ID']))
        client = str(UUID(os.environ['MICROSOFT_CLIENT_ID']))
        secret = os.environ['MICROSOFT_CLIENT_SECRET']
        redirect = os.environ['MICROSOFT_REDIRECT_URI']
        key = os.environ['MICROSOFT_FERNET_KEY']
        Fernet(key.encode('ascii'))
        url = urlsplit(redirect)
        if (not secret.strip() or not url.hostname or url.username is not None or url.password is not None
                or url.fragment or url.query or url.path not in ('/integrations/microsoft/callback', '/api/integrations/microsoft/callback')
                or any(ord(c) <= 32 or ord(c) == 127 for c in redirect)
                or '\\' in redirect or url.port is not None and not 1 <= url.port <= 65535
                or not (url.scheme == 'https' or (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')))):
            return None
        return Config(tenant, client, secret, redirect, key)
    except (KeyError, ValueError, UnicodeError):
        return None

def get_session_factory():
    return SessionLocal

def get_http_transport():
    return None

def required_config():
    config = get_config()
    if config is None:
        raise HTTPException(status_code=503, detail='Integración Microsoft no disponible', headers=HEADERS)
    return config

@router.get('/admin/integrations/microsoft')
def status(_=Depends(admin_user), factory=Depends(get_session_factory)):
    config = get_config()
    if config is None:
        return JSONResponse({'configured': False, 'connected': False, 'account_label': None, 'needs_reconnect': False}, headers=HEADERS)
    with factory() as db:
        credential = db.get(MicrosoftCredential, 1)
        data = {'configured': True, 'connected': credential is not None,
                'account_label': credential.account_label if credential else None,
                'needs_reconnect': credential.needs_reconnect if credential else False}
    return JSONResponse(data, headers=HEADERS)

@router.post('/admin/integrations/microsoft/start')
def start(admin=Depends(admin_user), factory=Depends(get_session_factory)):
    config = required_config()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    state, browser = create_oauth_state(factory, config.key, admin.id, verifier)
    query = urlencode({'client_id': config.client, 'response_type': 'code', 'redirect_uri': config.redirect,
                       'response_mode': 'query', 'scope': SCOPES, 'state': state,
                       'code_challenge': challenge, 'code_challenge_method': 'S256'})
    response = JSONResponse({'authorization_url': f'https://login.microsoftonline.com/{config.tenant}/oauth2/v2.0/authorize?{query}'}, headers=HEADERS)
    response.set_cookie(COOKIE, browser, max_age=600, path=urlsplit(config.redirect).path, httponly=True,
                        secure=urlsplit(config.redirect).scheme == 'https', samesite='lax')
    return response

def finish(success=False, secure=True, path='/integrations/microsoft/callback'):
    response = RedirectResponse('/admin?microsoft=' + ('connected' if success else 'error'), status_code=303, headers=HEADERS)
    response.delete_cookie(COOKIE, path=path, secure=secure, httponly=True, samesite='lax')
    return response

@router.get('/integrations/microsoft/callback')
def callback(request: Request, factory=Depends(get_session_factory), transport=Depends(get_http_transport)):
    config = get_config()
    if config is None:
        return finish()
    secure = urlsplit(config.redirect).scheme == 'https'
    path = urlsplit(config.redirect).path
    try:
        state = request.query_params.get('state', '')
        browser = request.cookies.get(COOKIE, '')
        consumed = consume_oauth_state(factory, config.key, state, browser)
        if not consumed:
            return finish(secure=secure, path=path)
        admin_id, verifier, generation = consumed
        with factory() as db:
            admin = db.get(User, admin_id)
            if not admin or not admin.is_active or admin.role != 'admin':
                return finish(secure=secure, path=path)
        code = request.query_params.get('code', '')
        if request.query_params.get('error') or not code or len(code) > 8192:
            return finish(secure=secure, path=path)
        with httpx.Client(transport=transport, follow_redirects=False, timeout=10.0) as client:
            token_response = client.post(f'https://login.microsoftonline.com/{config.tenant}/oauth2/v2.0/token', data={
                'client_id': config.client, 'client_secret': config.secret, 'grant_type': 'authorization_code',
                'code': code, 'redirect_uri': config.redirect, 'code_verifier': verifier, 'scope': SCOPES})
            token_response.raise_for_status()
            tokens = token_response.json()
            access, refresh = tokens['access_token'], tokens['refresh_token']
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
                return finish(secure=secure, path=path)
            me_response = client.get('https://graph.microsoft.com/v1.0/me', headers={'Authorization': f'Bearer {access}'})
            me_response.raise_for_status()
            me = me_response.json()
            object_id = str(UUID(me['id']))
            label = me.get('displayName') or me.get('userPrincipalName') or object_id
            if not isinstance(label, str) or len(label) > 255:
                return finish(secure=secure, path=path)
        ciphertext = Fernet(config.key.encode('ascii')).encrypt(refresh.encode()).decode('ascii')
        with factory.begin() as db:
            binding = lock_generation(db)
            if binding.generation != generation:
                return finish(secure=secure, path=path)
            if binding.account_object_id is not None or binding.tenant_id is not None:
                if binding.account_object_id != object_id or binding.tenant_id != config.tenant:
                    return finish(secure=secure, path=path)
            admin = db.get(User, admin_id)
            if not admin or not admin.is_active or admin.role != 'admin':
                return finish(secure=secure, path=path)
            credential = db.execute(select(MicrosoftCredential).where(MicrosoftCredential.id == 1).with_for_update()).scalar_one_or_none()
            if binding.account_object_id is None:
                # An experimental pre-binding credential cannot establish a trusted tenant.
                if credential is not None:
                    return finish(secure=secure, path=path)
                binding.account_object_id = object_id
                binding.tenant_id = config.tenant
            if credential is None:
                credential = MicrosoftCredential(id=1, refresh_token_ciphertext=ciphertext)
                db.add(credential)
            else:
                # Fence any worker whose refresh request is still in flight.
                credential.refresh_generation += 1
                credential.refresh_lease_id = None
                credential.refresh_lease_expires_at = None
            credential.refresh_token_ciphertext = ciphertext
            credential.account_object_id = object_id
            credential.account_label = label
            credential.needs_reconnect = False
            binding.generation += 1
        return finish(True, secure, path)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError, InvalidToken):
        return finish(secure=secure, path=path)

@router.post('/admin/integrations/microsoft/disconnect')
def disconnect(_=Depends(admin_user), factory=Depends(get_session_factory)):
    with factory.begin() as db:
        generation = lock_generation(db)
        generation.generation += 1
        db.execute(delete(MicrosoftCredential))
        db.execute(delete(MicrosoftOAuthState))
    response = JSONResponse({'connected': False}, headers=HEADERS)
    config = get_config()
    response.delete_cookie(COOKIE, path=urlsplit(config.redirect).path if config else '/integrations/microsoft/callback', secure=not config or urlsplit(config.redirect).scheme == 'https', httponly=True, samesite='lax')
    return response
