import httpx
from sqlalchemy import select
from ephemeral_postgres import postgres_test_db

from app import teams_sync
from app.teams_models import TeamsSyncJob
from test_teams_recreation_worker_authorization import setup_job, decision, authorize
from teams_identity_fixtures import IDENTITY
from app.microsoft_auth_models import MicrosoftOAuthGeneration
from app.microsoft_refresh import MicrosoftAccessTokenIdentity


def test_legacy_recreation_authorization_cannot_post(postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        legacy = decision(job.id)
        legacy.tenant_id = legacy.account_object_id = None
        db.add(legacy)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201, json={'id': 'event'}))) as client:
        teams_sync.process_pending(sessions, lambda: IDENTITY, client)
    assert not calls
    with sessions() as db:
        assert db.scalar(select(TeamsSyncJob)).event_id is None


def test_same_account_reconnect_uses_new_generation(postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        authorize(job)
        db.get(MicrosoftOAuthGeneration, 1).generation = 5
    calls = []
    identity = MicrosoftAccessTokenIdentity('fake-token', IDENTITY.tenant_id, IDENTITY.account_object_id, 5)
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201, json={'id': 'event'}))) as client:
        teams_sync.process_pending(sessions, lambda: identity, client)
    assert [r.method for r in calls] == ['POST']


def test_generation_change_during_token_acquisition_fences_post(postgres_test_db):
    sessions = setup_job(postgres_test_db)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob))
        authorize(job)
    calls = []
    def stale_token():
        with sessions.begin() as db:
            db.get(MicrosoftOAuthGeneration, 1).generation = 5
        return IDENTITY
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201))) as client:
        teams_sync.process_pending(sessions, stale_token, client)
    assert calls == []


def test_upgrade_adds_identity_without_inventing_legacy_owner(postgres_test_db):
    from sqlalchemy import inspect, text
    from app.database import _upgrade_microsoft_schema
    engine = postgres_test_db.engine
    with engine.begin() as db:
        db.execute(text('CREATE TABLE teams_recreation_decisions (id INTEGER PRIMARY KEY)'))
        db.execute(text('INSERT INTO teams_recreation_decisions (id) VALUES (1)'))
        _upgrade_microsoft_schema(db, 'postgresql')
        _upgrade_microsoft_schema(db, 'postgresql')
        assert db.execute(text('SELECT id FROM teams_recreation_decisions')).one() == (1,)
        assert {c['name'] for c in inspect(db).get_columns('teams_recreation_decisions')} == {'id'}
