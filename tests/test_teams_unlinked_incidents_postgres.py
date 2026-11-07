from threading import Event, Thread
import httpx
from sqlalchemy import select
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob, utcnow
from test_teams_postgres import sessions
from test_teams_delete_races_postgres import api, make_event, worker


def test_late_patch_reopens_unlinked_notice(sessions, api):
    client, (admin, user), headers = api
    bid = make_event(sessions, user)
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).applied_revision = 0
    entered, release, errors = Event(), Event(), []
    def graph(request):
        entered.set()
        assert release.wait(15)
        return httpx.Response(200, json={})
    def run():
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(15)
        with sessions.begin() as db:
            op = db.scalar(select(TeamsRemoteOperation).with_for_update())
            op.state = 'unknown'
            op.reviewed_at = utcnow()
            op.reviewed_by_admin_id = admin
            op.reviewed_revision = 1
            op.reviewed_desired = 'delete'
            job = db.get(TeamsSyncJob, op.job_id)
            job.desired, job.revision, job.applied_revision = 'delete', 2, 2
            job.event_id = job.join_url = job.lease_token = job.lease_expires_at = None
        response = client.delete(f'/admin/classes/{bid}', headers=headers)
        assert response.status_code == 204, response.text
        assert client.get('/admin/teams/unlinked-incidents', headers=headers).json()['count'] == 0
    finally:
        release.set()
        thread.join(20)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        assert db.scalar(select(TeamsRemoteOperation)).reviewed_at is None
    result = client.get('/admin/teams/unlinked-incidents', headers=headers).json()
    assert result['count'] == 1
    assert result['incidents'][0]['reason'] == 'patch_unresolved'
