import pytest

from app.teams_review_routes import router
from test_teams_api import context, auth


@pytest.mark.parametrize('suffix', ['search', 'resolve'])
def test_abandoned_review_route_is_not_registered(suffix):
    path = f'/admin/classes/{{booking_id}}/teams/review/{suffix}'
    assert not any(route.path == path for route in router.routes)


@pytest.mark.parametrize('suffix', ['search', 'resolve'])
def test_abandoned_review_endpoint_returns_404(context, suffix):
    _, client, (admin, _, _), bid = context
    response = client.post(f'/admin/classes/{bid}/teams/review/{suffix}', headers=auth(admin), json={})
    assert response.status_code == 404


def test_recreate_route_remains_registered():
    assert any(route.path == '/admin/classes/{booking_id}/teams/recreate' for route in router.routes)
