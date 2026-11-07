from sqlalchemy import select
from app.security import create_token
from app.teams_models import TeamsSyncJob
from test_teams_delete_hooks import ctx, add_booking


def test_unlinked_incident_authorization_and_privacy(ctx):
    sessions, client, (admin, student) = ctx
    with sessions.begin() as db:
        add_booking(db, student)
        job = db.scalar(select(TeamsSyncJob))
        job.booking_id = None
        job.desired = 'delete'
        job.last_error = 'secret-token https://private.example'
        job_id = job.id
    path = '/admin/teams/unlinked-incidents'
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': f'Bearer {create_token(student)}'}).status_code == 403
    response = client.get(path, headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == 200
    assert response.json()['count'] == 1
    assert response.json()['incidents'][0]['job_id'] == job_id
    assert set(response.json()['incidents'][0]) == {'job_id', 'starts_at', 'reason'}
    assert 'secret' not in response.text and 'private.example' not in response.text


def test_tombstone_patch_review_and_invalidation(ctx):
    from app.teams_models import TeamsRemoteOperation, utcnow
    sessions, client, (admin, student) = ctx
    headers = {'Authorization': f'Bearer {create_token(admin)}'}
    path = '/admin/teams/unlinked-incidents'
    with sessions.begin() as db:
        bid = add_booking(db, student)
        job = db.scalar(select(TeamsSyncJob))
        job.desired, job.applied_revision = 'delete', job.revision
        job_id = job.id
        db.add(TeamsRemoteOperation(id='op', job_id=job.id, operation='patch', state='unknown', event_id='private-event', transaction_revision=1, tenant_id='tenant', account_object_id='account', reviewed_at=utcnow(), reviewed_by_admin_id=admin, reviewed_revision=1, reviewed_desired='delete'))
    assert client.delete(f'/admin/classes/{bid}', headers=headers).status_code == 204
    assert client.get(path, headers=headers).json() == {'count': 0, 'incidents': []}
    with sessions.begin() as db:
        db.scalar(select(TeamsRemoteOperation)).reviewed_at = None
    result = client.get(path, headers=headers).json()
    assert result['count'] == 1
    assert result['incidents'][0]['job_id'] == job_id
    assert result['incidents'][0]['reason'] == 'patch_unresolved'


def test_incident_categories_and_limit(ctx):
    from uuid import uuid4
    from app.teams_models import TeamsCreationAttempt
    sessions, client, (admin, student) = ctx
    with sessions.begin() as db:
        for i in range(103):
            job = TeamsSyncJob(booking_key=str(i), booking_id=None, desired='delete', transaction_id=str(uuid4()), revision=1, applied_revision=1)
            db.add(job); db.flush()
            if i == 0:
                db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=1, state='unknown'))
            elif i == 1:
                db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=1, state='identified', event_id='private', needs_review=True))
            elif i < 102:
                job.applied_revision = 0
    result = client.get('/admin/teams/unlinked-incidents', headers={'Authorization': f'Bearer {create_token(admin)}'}).json()
    assert result['count'] == 102 and len(result['incidents']) == 100
    assert [row['reason'] for row in result['incidents'][:3]] == ['creation_unresolved', 'orphan_unresolved', 'delete_unresolved']
