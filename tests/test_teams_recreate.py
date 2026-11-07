from sqlalchemy import select
from test_teams_api import auth, context
from teams_recreate_fixtures import review
from app.teams_models import TeamsSyncJob, TeamsCreationAttempt
from app.microsoft_auth_models import MicrosoftOAuthGeneration
from datetime import datetime, timedelta, timezone
import pytest
from uuid import uuid4
from app.models import Booking


def test_account_mismatch_reconnect_retry_dispatches_to_original(review):
    import httpx
    from app import teams_sync
    from app.microsoft_refresh import MicrosoftAccessTokenIdentity
    from app.microsoft_auth_models import MicrosoftCredential
    sessions, client, admin, bid, _, _, _ = review
    tenant = '11111111-1111-4111-8111-111111111111'
    original = '33333333-3333-4333-8333-333333333333'
    other = '44444444-4444-4444-8444-444444444444'
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.event_id = None
        job.creation_in_flight = True
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id, revision=job.revision,
                                    tenant_id=tenant, account_object_id=original, oauth_generation=3, state='unknown'))
    path = f'/admin/classes/{bid}/teams/recreate'
    assert client.post(path, headers=auth(admin), json={'expected_revision': 2, 'confirm_absent': True}).status_code == 200
    with sessions.begin() as db:
        db.get(MicrosoftOAuthGeneration, 1).account_object_id = other
        db.get(MicrosoftCredential, 1).account_object_id = other
    calls = []
    def graph(request):
        calls.append(request)
        return httpx.Response(201, json={'id': 'new-event', 'onlineMeeting': {'joinUrl': 'https://teams.microsoft.com/meeting'}})
    with httpx.Client(transport=httpx.MockTransport(graph)) as graph_client:
        teams_sync.process_pending(sessions, lambda: MicrosoftAccessTokenIdentity('wrong', tenant, other, 3), graph_client)
        assert not calls
        assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 409
        with sessions.begin() as db:
            db.get(MicrosoftOAuthGeneration, 1).account_object_id = original
            db.get(MicrosoftCredential, 1).account_object_id = original
        assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 200
        teams_sync.process_pending(sessions, lambda: MicrosoftAccessTokenIdentity('original', tenant, original, 3), graph_client)
    assert len(calls) == 1 and calls[0].headers['authorization'] == 'Bearer original'


def test_account_mismatch_is_visible_and_retry_cannot_bypass(review):
    sessions, client, admin, bid, _, _, _ = review
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.last_error = 'recreation_account_mismatch'
        job.creation_in_flight = False
    row = next(row for row in client.get('/admin/classes', headers=auth(admin)).json() if row['id'] == bid)
    assert row['teams_review_reason'] == 'recreation_account_mismatch'
    assert row['teams_status'] == 'needs_review'
    assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 409


def test_admin_class_exposes_revision_and_unresolved_attempts(review):
    sessions, client, admin, bid, _, _, _ = review
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id,
            revision=job.revision, tenant_id='11111111-1111-4111-8111-111111111111',
            account_object_id='33333333-3333-4333-8333-333333333333', oauth_generation=3, state='unknown'))
    row = next(row for row in client.get('/admin/classes', headers=auth(admin)).json() if row['id'] == bid)
    assert row['teams_revision'] == 2
    assert row['teams_unresolved_attempts'] == 1


def test_recreate_requires_exact_confirmation_and_preserves_attempt(review):
    sessions, client, admin, bid, _, _, _ = review
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.event_id = None
        job.creation_in_flight = True
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id, revision=job.revision,
               tenant_id='11111111-1111-4111-8111-111111111111',
               account_object_id='33333333-3333-4333-8333-333333333333', oauth_generation=3, state='unknown'))
    path = f'/admin/classes/{bid}/teams/recreate'
    assert client.post(path, headers=auth(admin), json={'expected_revision': 2, 'confirm_absent': False}).status_code == 422
    assert client.post(path, headers=auth(admin), json={'expected_revision': 1, 'confirm_absent': True}).status_code == 409
    result = client.post(path, headers=auth(admin), json={'expected_revision': 2, 'confirm_absent': True})
    assert result.status_code == 200, result.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        attempt = db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.job_id == job.id, TeamsCreationAttempt.state == 'unknown'))
        assert attempt.state == 'unknown' and job.transaction_id != attempt.transaction_id
        assert job.revision == 3 and not job.creation_in_flight and job.last_error is None
        assert db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.transaction_id == job.transaction_id)) is None
        assert not list(db.scalars(select(TeamsCreationAttempt).where(TeamsCreationAttempt.state == 'rejected')))
        assert job.recreation_transaction_id == job.transaction_id
        assert job.recreation_admin_id == admin and job.recreation_reason == 'creation_result_unknown'
    assert client.post(path, headers=auth(admin), json={'expected_revision': 2, 'confirm_absent': True}).status_code == 409


@pytest.mark.parametrize('mutation', ['lease', 'account', 'missing_identity', 'known_event'])
def test_recreate_rejects_unsafe_state_without_network(review, mutation):
    sessions, client, admin, bid, _, state, _ = review
    state['on_graph'] = lambda: pytest.fail('Graph must not be called')
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        if mutation == 'lease':
            job.lease_token = 'lease'
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        elif mutation == 'account':
            db.get(MicrosoftOAuthGeneration, 1).account_object_id = '44444444-4444-4444-8444-444444444444'
        elif mutation == 'missing_identity':
            db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=job.transaction_id,
                revision=job.revision, state='unknown'))
        else:
            job.event_id = 'unverified'
    result = client.post(f'/admin/classes/{bid}/teams/recreate', headers=auth(admin),
                         json={'expected_revision': 2, 'confirm_absent': True})
    assert result.status_code == 409
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.revision == 2
        assert db.scalar(select(TeamsCreationAttempt.id).where(TeamsCreationAttempt.authorized_at.is_not(None))) is None


def test_missing_event_without_verifiable_identity_requires_support(review):
    sessions, client, admin, bid, _, _, _ = review
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.last_error = 'event_missing'
        job.event_id = None
    response = client.post(f'/admin/classes/{bid}/teams/recreate', headers=auth(admin),
                           json={'expected_revision': 2, 'confirm_absent': True})
    assert response.status_code == 409
    assert 'soporte' in response.json()['detail']


def test_missing_event_preserves_audit_and_manual_comment(review):
    sessions, client, admin, bid, _, state, _ = review
    state['on_graph'] = lambda: pytest.fail('Graph must not be called')
    url = 'https://teams.microsoft.com/l/meetup-join/old'
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.last_error = 'event_missing'
        job.event_id, job.join_url = 'old-event', url
        db.get(Booking, bid).admin_comment = f'Manual\nEnlace de Teams: {url}'
        db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=job.revision,
            tenant_id='11111111-1111-4111-8111-111111111111',
            account_object_id='33333333-3333-4333-8333-333333333333', oauth_generation=3,
            state='identified', event_id='old-event'))
    result = client.post(f'/admin/classes/{bid}/teams/recreate', headers=auth(admin),
                         json={'expected_revision': 2, 'confirm_absent': True})
    assert result.status_code == 200, result.text
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert (job.recreation_prior_event_id, job.recreation_prior_join_url) == ('old-event', url)
        assert job.event_id is None and job.join_url is None
        assert db.get(Booking, bid).admin_comment == 'Manual'
