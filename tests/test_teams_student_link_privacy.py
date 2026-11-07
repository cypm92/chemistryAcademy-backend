import pytest
from sqlalchemy import select

from test_teams_api import context, auth
from app import models
from app.teams_models import TeamsSyncJob


def test_pending_generated_link_is_not_exposed_to_student(context):
    sessions, client, (admin, student, _), bid = context
    url = 'https://example.test/join'
    comment = 'Nota y enlace manual https://manual.test\nEnlace de Teams: ' + url
    with sessions.begin() as db:
        db.get(models.Booking, bid).admin_comment = comment
    student_response = client.get('/classes', headers=auth(student))
    assert student_response.status_code == 200
    assert url not in student_response.text
    assert student_response.json()[0]['admin_comment'] == 'Nota y enlace manual https://manual.test'
    assert client.get('/admin/classes', headers=auth(admin)).json()[0]['admin_comment'] == comment


@pytest.mark.parametrize('applied,desired,status,error,visible', [
    (1, 'upsert', 'confirmed', None, False),
    (1, 'upsert', 'confirmed', 'graph_error_503', False),
    (2, 'upsert', 'confirmed', 'event_missing', False),
    (2, 'upsert', 'confirmed', 'manual_reconcile', False),
    (2, 'delete', 'cancelled', None, False),
    (2, 'upsert', 'cancelled', None, False),
    (2, 'upsert', 'confirmed', None, True),
])
def test_generated_visibility_states(context, applied, desired, status, error, visible):
    sessions, client, (admin, student, _), bid = context
    url = 'https://example.test/join'
    manual = 'x' * 1000 + '\nEnlace de Teams: https://manual.test/join\n{teams_link}'
    comment = manual + '\nEnlace de Teams: ' + url
    with sessions.begin() as db:
        booking = db.get(models.Booking, bid)
        booking.status, booking.admin_comment = status, comment
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid))
        job.applied_revision, job.desired, job.last_error = applied, desired, error
    response = client.get('/classes', headers=auth(student))
    assert response.status_code == 200
    row = response.json()[0]
    assert row['admin_comment'] == (comment if visible else manual)
    assert (url in response.text) == visible
    assert row['teams_status'] is None and row['teams_revision'] is None
    assert not error or error not in response.text
    assert client.get('/admin/classes', headers=auth(admin)).json()[0]['admin_comment'] == comment


def test_no_job_preserves_manual_comment(context):
    sessions, client, (_, student, _), bid = context
    manual = 'Manual\nEnlace de Teams: https://manual.test/join\n{teams_link}'
    with sessions.begin() as db:
        db.get(models.Booking, bid).admin_comment = manual
        db.delete(db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == bid)))
    assert client.get('/classes', headers=auth(student)).json()[0]['admin_comment'] == manual
