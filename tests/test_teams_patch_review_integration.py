import pytest
from sqlalchemy import select
from app import main, models
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob, utcnow
from test_teams_delete_hooks import ctx, add_booking


@pytest.mark.parametrize('state,decision,expected', [
    ('unknown', 'active', False), ('unknown', 'invalid', True),
    ('in_flight', 'active', True),
])
def test_review_only_resolves_matching_unknown(ctx, state, decision, expected):
    sessions, _, (admin, user) = ctx
    with sessions.begin() as db:
        bid = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        job.applied_revision = job.revision
        job.join_url = 'https://example.test/join'
        booking = db.get(models.Booking, bid)
        booking.admin_comment = 'Enlace de Teams: ' + job.join_url
        db.add(TeamsRemoteOperation(id='operation', job_id=job.id, operation='patch', state=state,
            event_id='event', transaction_revision=1, tenant_id='tenant', account_object_id='account',
            reviewed_at=utcnow() if decision == 'active' else None, reviewed_by_admin_id=admin,
            reviewed_revision=1, reviewed_desired='upsert'))
    with sessions() as db:
        booking = db.get(models.Booking, bid)
        admin_data = main.class_data(booking)
        student_data = main.class_data(booking, student=True)
        assert (admin_data['teams_status'] == 'needs_review') is expected
        assert ('https://example.test/join' not in student_data['admin_comment']) is expected


def test_late_callback_invalidates_review_without_overwriting_job(ctx, monkeypatch):
    import httpx
    from app import microsoft_graph as graph
    from app.teams_sync import process_pending
    from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
    sessions, _, (admin, user) = ctx
    with sessions.begin() as db:
        seed_account(db)
        bid = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        job.event_id, job.join_url = 'event', 'https://example.test/join'
        seed_identified_event(db, job)
    def late(*args):
        with sessions.begin() as db:
            operation = db.scalar(select(TeamsRemoteOperation))
            operation.state = 'unknown'
            operation.reviewed_at = utcnow()
            operation.reviewed_by_admin_id = admin
            operation.reviewed_revision = 1
            operation.reviewed_desired = 'upsert'
            job = db.get(TeamsSyncJob, operation.job_id)
            job.desired, job.revision, job.applied_revision = 'delete', 2, 2
        return 'https://example.test/join'
    monkeypatch.setattr(graph, 'update_event', late)
    with httpx.Client() as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
    with sessions() as db:
        operation = db.scalar(select(TeamsRemoteOperation))
        job = db.scalar(select(TeamsSyncJob))
        assert operation.reviewed_at is None
        assert operation.state == 'unknown'
        assert (job.desired, job.revision, job.applied_revision) == ('delete', 2, 2)
        assert main.class_data(db.get(models.Booking, bid))['teams_status'] == 'needs_review'


@pytest.mark.parametrize('state,invalid,expected', [('unknown', False, 204), ('unknown', True, 409), ('in_flight', False, 409)])
def test_delete_guard_obeys_review(ctx, state, invalid, expected):
    from app.security import create_token
    sessions, client, (admin, user) = ctx
    with sessions.begin() as db:
        bid = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        job.desired, job.applied_revision = 'delete', job.revision
        db.add(TeamsRemoteOperation(id='op', job_id=job.id, operation='patch', state=state,
            event_id='event', transaction_revision=1, tenant_id='tenant', account_object_id='account',
            reviewed_at=None if invalid else utcnow(), reviewed_by_admin_id=admin,
            reviewed_revision=1, reviewed_desired='upsert'))
    response = client.delete(f'/admin/classes/{bid}', headers={'Authorization': f'Bearer {create_token(admin)}'})
    assert response.status_code == expected, response.text


def test_review_does_not_hide_other_unknown_patch(ctx):
    sessions, _, (admin, user) = ctx
    with sessions.begin() as db:
        bid = add_booking(db, user)
        job = db.scalar(select(TeamsSyncJob))
        for op in ('reviewed', 'other'):
            db.add(TeamsRemoteOperation(id=op, job_id=job.id, operation='patch', state='unknown',
                event_id='event', transaction_revision=1, tenant_id='tenant', account_object_id='account'))
        db.flush()
        op = db.get(TeamsRemoteOperation, 'reviewed')
        op.reviewed_at, op.reviewed_by_admin_id = utcnow(), admin
        op.reviewed_revision, op.reviewed_desired = 1, 'upsert'
    with sessions() as db:
        result = main.class_data(db.get(models.Booking, bid))
        assert result['teams_status'] == 'needs_review'
        assert result['teams_unresolved_attempts'] == 1
