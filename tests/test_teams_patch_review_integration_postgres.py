from threading import Event, Thread
import httpx
from sqlalchemy import select
from app import main, models
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob, utcnow
from test_teams_postgres import sessions
from test_teams_delete_races_postgres import api, make_event, worker


def test_reviewed_patch_late_callback_reopens_without_changing_delete(sessions, api):
    _, (admin, user), _ = api
    bid = make_event(sessions, user)
    with sessions.begin() as db:
        db.scalar(select(TeamsSyncJob)).applied_revision = 0
    entered, release = Event(), Event()
    errors = []
    def graph(request):
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json={})
    def run():
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            operation = db.scalar(select(TeamsRemoteOperation).with_for_update())
            operation.state = 'unknown'
            operation.reviewed_at = utcnow()
            operation.reviewed_by_admin_id = admin
            operation.reviewed_revision = 1
            operation.reviewed_desired = 'upsert'
            job = db.get(TeamsSyncJob, operation.job_id)
            job.desired, job.revision, job.applied_revision = 'delete', 2, 2
        with sessions() as db:
            assert main.class_data(db.get(models.Booking, bid))['teams_status'] != 'needs_review'
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        operation = db.scalar(select(TeamsRemoteOperation))
        job = db.scalar(select(TeamsSyncJob))
        assert operation.reviewed_at is None and operation.state == 'unknown'
        assert (job.desired, job.revision, job.applied_revision) == ('delete', 2, 2)
        assert main.class_data(db.get(models.Booking, bid))['teams_status'] == 'needs_review'
