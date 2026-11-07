import pytest
from sqlalchemy import select

from app import models
from app.teams_models import TeamsSyncJob
from test_teams_api import context, auth


@pytest.mark.parametrize('desired', ['upsert', 'delete'])
def test_unknown_creation_requires_review_and_cannot_retry(context, desired):
    sessions, client, (admin, student, _), bid = context
    with sessions.begin() as db:
        db.get(models.Booking, bid).status = 'confirmed' if desired == 'upsert' else 'cancelled'
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.desired = desired
        job.last_error = 'creation_result_unknown'
        job.creation_in_flight = True
    row = client.get('/admin/classes', headers=auth(admin)).json()[0]
    assert row['teams_status'] == 'needs_review'
    assert row['teams_action'] == desired
    student_row = client.get('/classes', headers=auth(student)).json()[0]
    assert student_row['teams_status'] is None
    assert 'creation_result_unknown' not in str(student_row)
    assert client.post(f'/admin/classes/{bid}/teams/retry', headers=auth(admin)).status_code == 409
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        assert job.last_error == 'creation_result_unknown'
        assert job.creation_in_flight is True
