"""Durability and safety of PATCH requests against PostgreSQL."""
from datetime import timedelta
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import select

from app import main, models
from app.teams_models import TeamsRemoteOperation, TeamsSyncJob, utcnow
from test_teams_delete_races_postgres import api, make_event, worker
from test_teams_postgres import sessions


@pytest.mark.parametrize('outcome,state', [('success', 'completed'), ('reject', 'completed'), ('server', 'unknown'), ('transport', 'unknown')])
def test_late_patch_result_is_durable_and_blocks_user_delete(sessions, api, outcome, state):
    client, (_, user), headers = api
    bid = make_event(sessions, user)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.applied_revision = 0
        jid = job.id
    entered, release = Event(), Event()
    errors = []
    def graph(req):
        entered.set()
        assert release.wait(10)
        if outcome == 'transport':
            raise httpx.ReadTimeout('sensitive', request=req)
        return httpx.Response({'success': 200, 'reject': 403, 'server': 500}[outcome], json={})
    def run():
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run); thread.start()
    try:
        assert entered.wait(10)
        with sessions.begin() as db:
            operation = db.scalar(select(TeamsRemoteOperation))
            assert (operation.job_id, operation.state, operation.event_id) == (jid, 'in_flight', 'remote-event')
            db.get(TeamsSyncJob, jid).lease_expires_at = utcnow() - timedelta(seconds=1)
        assert client.post(f'/admin/classes/{bid}/cancel', headers=headers).status_code == 200
        assert worker(sessions, lambda req: httpx.Response(204))
        assert client.delete(f'/admin/users/{user}', headers=headers).status_code == 409
    finally:
        release.set(); thread.join(15)
    assert not thread.is_alive() and not errors
    with sessions() as db:
        operation = db.scalar(select(TeamsRemoteOperation))
        assert operation.state == state
        if state == 'unknown':
            assert main.class_data(db.get(models.Booking, bid))['teams_status'] == 'needs_review'
    if state == 'unknown':
        assert client.delete(f'/admin/classes/{bid}', headers=headers).status_code == 409
    else:
        assert client.delete(f'/admin/classes/{bid}', headers=headers).status_code == 204


def test_two_overlapping_patches_keep_distinct_results(sessions, api):
    client, (_, user), headers = api
    bid = make_event(sessions, user)
    with sessions.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.applied_revision = 0
        jid = job.id
    entered = [Event(), Event()]
    release = [Event(), Event()]
    errors = []
    def run(index):
        def graph(req):
            entered[index].set()
            assert release[index].wait(10)
            return httpx.Response(200 if index else 500, json={})
        try:
            worker(sessions, graph)
        except BaseException as exc:
            errors.append(exc)
    first = Thread(target=run, args=(0,)); second = Thread(target=run, args=(1,))
    first.start()
    try:
        assert entered[0].wait(10)
        with sessions.begin() as db:
            db.get(TeamsSyncJob, jid).lease_expires_at = utcnow() - timedelta(seconds=1)
        second.start()
        assert entered[1].wait(10)
        with sessions() as db:
            operations = list(db.scalars(select(TeamsRemoteOperation)))
            assert len(operations) == 2 and len({op.id for op in operations}) == 2
            assert all(op.state == 'in_flight' for op in operations)
        release[1].set(); second.join(15)
        release[0].set(); first.join(15)
        assert not errors and not first.is_alive() and not second.is_alive()
        with sessions() as db:
            assert sorted(op.state for op in db.scalars(select(TeamsRemoteOperation))) == ['completed', 'unknown']
            assert main.class_data(db.get(models.Booking, bid))['teams_status'] == 'needs_review'
    finally:
        release[0].set(); release[1].set(); first.join(15)
        if second.ident is not None:
            second.join(15)
