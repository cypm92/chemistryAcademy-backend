from test_material_metadata import setup, auth
from app import models


def test_legacy_unclassified_and_default_folder_order_together(setup):
    client, sessions, _, ids = setup
    with sessions.begin() as db:
        unclassified = models.Folder(name='Sin clasificar')
        db.add(unclassified); db.flush()
        db.get(models.Material, ids['material']).folder_id = unclassified.id
    def order():
        return [m['id'] for m in client.get('/admin/materials', headers=auth(ids['admin'])).json()
                if m['id'] in (ids['material'], ids['other'])]
    before = order()
    result = client.post(f"/admin/materials/{before[1]}/order", json={'direction': 'up'}, headers=auth(ids['admin']))
    assert result.status_code == 204
    assert order() == before[::-1]
