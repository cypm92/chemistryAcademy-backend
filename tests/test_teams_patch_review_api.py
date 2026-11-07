from uuid import uuid4
from datetime import datetime, timedelta, timezone

import pytest
from app.models import Booking
from app.microsoft_auth_models import MicrosoftOAuthGeneration
from app.teams_models import TeamsCreationAttempt

from sqlalchemy import select

from test_teams_api import auth, context
from teams_recreate_fixtures import review
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob

TENANT = '11111111-1111-4111-8111-111111111111'
ACCOUNT = '33333333-3333-4333-8333-333333333333'
BODY = {'expected_revision': 2, 'acknowledge_checked': True, 'acknowledge_late_risk': True}


def seed(review):
    sessions, client, admin, bid, _, state, _ = review
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.event_id = 'event-1'
        job.last_error = 'worker_error'
        db.add(TeamsRemoteOperation(id=str(uuid4()), job_id=job.id, operation='patch', state='unknown',
            event_id='event-1', transaction_revision=1, tenant_id=TENANT, account_object_id=ACCOUNT, oauth_generation=3))
    return sessions, client, admin, bid


def test_review_is_audited_and_replay_rejected(review):
    sessions, client, admin, bid = seed(review)
    path = f'/admin/classes/{bid}/teams/patch-review'
    response = client.post(path, headers=auth(admin), json=BODY)
    assert response.status_code == 200, response.text
    assert client.post(path, headers=auth(admin), json=BODY).status_code == 409
    with sessions() as db:
        op = db.scalar(select(TeamsRemoteOperation))
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert op.state == 'unknown'
        assert op.reviewed_at is not None
        assert op.reviewed_by_admin_id == admin
        assert op.reviewed_revision == 2 and op.reviewed_desired == 'upsert'
        assert job.last_error == 'worker_error'


def test_review_only_reopened_operation_without_rewriting_other_review(review):
    sessions, client, admin, bid = seed(review)
    with sessions.begin() as db:
        original = db.scalar(select(TeamsRemoteOperation))
        original_id = original.id
        reopened_id = str(uuid4())
        db.add(TeamsRemoteOperation(id=reopened_id, job_id=original.job_id, operation='patch', state='unknown',
            event_id='event-1', transaction_revision=1, tenant_id=TENANT, account_object_id=ACCOUNT, oauth_generation=3))
    path = f'/admin/classes/{bid}/teams/patch-review'
    assert client.post(path, headers=auth(admin), json=BODY).status_code == 200
    with sessions.begin() as db:
        original = db.get(TeamsRemoteOperation, original_id)
        original_review = (original.reviewed_at, original.reviewed_by_admin_id, original.reviewed_revision, original.reviewed_desired)
        reopened = db.get(TeamsRemoteOperation, reopened_id)
        reopened.reviewed_at = reopened.reviewed_by_admin_id = reopened.reviewed_revision = reopened.reviewed_desired = None
    response = client.post(path, headers=auth(admin), json=BODY)
    assert response.status_code == 200, response.text
    assert client.post(path, headers=auth(admin), json=BODY).status_code == 409
    with sessions() as db:
        original = db.get(TeamsRemoteOperation, original_id)
        reopened = db.get(TeamsRemoteOperation, reopened_id)
        assert (original.reviewed_at, original.reviewed_by_admin_id, original.reviewed_revision, original.reviewed_desired) == original_review
        assert reopened.reviewed_at is not None
        assert (reopened.reviewed_by_admin_id, reopened.reviewed_revision, reopened.reviewed_desired) == (admin, 2, 'upsert')
        assert len(db.scalars(select(TeamsRemoteOperation)).all()) == 2


@pytest.mark.parametrize('field,value', [('acknowledge_checked', 1), ('acknowledge_checked', False), ('acknowledge_late_risk', 'true'), ('expected_revision', True), ('expected_revision', '2')])
def test_strict_payload(review, field, value):
    _, client, admin, bid = seed(review)
    assert client.post(f'/admin/classes/{bid}/teams/patch-review', headers=auth(admin), json={**BODY, field: value}).status_code == 422


@pytest.mark.parametrize('mutation', ['stale', 'in_flight', 'expired_in_flight', 'lease', 'account', 'identity', 'creation', 'critical', 'status', 'missing_time'])
def test_unsafe_states_rejected(review, mutation):
    sessions, client, admin, bid = seed(review)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        op = db.scalar(select(TeamsRemoteOperation))
        if mutation == 'stale': job.revision += 1
        elif mutation in ('in_flight', 'expired_in_flight'):
            op.state = 'in_flight'
            if mutation == 'expired_in_flight':
                job.lease_token = 'old'
                job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        elif mutation == 'lease':
            job.lease_token = 'active'
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        elif mutation == 'account': db.get(MicrosoftOAuthGeneration, 1).account_object_id = str(uuid4())
        elif mutation == 'identity': op.tenant_id = None
        elif mutation == 'creation': db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=2, state='unknown'))
        elif mutation == 'critical': job.last_error = 'event_missing'
        elif mutation == 'status': db.get(Booking, bid).status = 'cancelled'
        elif mutation == 'missing_time': job.starts_at = None
    assert client.post(f'/admin/classes/{bid}/teams/patch-review', headers=auth(admin), json=BODY).status_code == 409
    with sessions() as db:
        assert db.scalar(select(TeamsRemoteOperation)).reviewed_at is None


def test_cancelled_delete_and_authorization(review):
    sessions, client, admin, bid = seed(review)
    path = f'/admin/classes/{bid}/teams/patch-review'
    assert client.post(path, json=BODY).status_code == 401
    assert client.post(path, headers=auth(admin + 1), json=BODY).status_code == 403
    with sessions.begin() as db:
        db.get(Booking, bid).status = 'cancelled'
        db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)).desired = 'delete'
    assert client.post(path, headers=auth(admin), json=BODY).status_code == 200
    with sessions() as db:
        assert db.scalar(select(TeamsRemoteOperation)).reviewed_desired == 'delete'


def test_review_after_legacy_schema_upgrade_requires_new_acceptance(review):
    from sqlalchemy import text
    from app.database import ensure_application_schema
    sessions, client, admin, bid = seed(review)
    engine = sessions.kw['bind']
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE teams_patch_review_decisions (id INTEGER PRIMARY KEY, operation_id VARCHAR(36) NOT NULL)'))
        conn.execute(text('INSERT INTO teams_patch_review_decisions SELECT 1, id FROM teams_remote_operations'))
        for name in ('reviewed_at', 'reviewed_by_admin_id', 'reviewed_revision', 'reviewed_desired'):
            conn.execute(text(f'ALTER TABLE teams_remote_operations DROP COLUMN {name}'))
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with sessions() as db:
        assert db.scalar(select(TeamsRemoteOperation)).reviewed_at is None
    response = client.post(f'/admin/classes/{bid}/teams/patch-review', headers=auth(admin), json=BODY)
    assert response.status_code == 200, response.text
    with sessions() as db:
        op = db.scalar(select(TeamsRemoteOperation))
        assert op.state == 'unknown' and op.reviewed_at is not None
        assert (op.reviewed_by_admin_id, op.reviewed_revision, op.reviewed_desired) == (admin, 2, 'upsert')
        assert db.execute(text('SELECT count(*) FROM teams_patch_review_decisions')).scalar_one() == 1
