from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from ephemeral_postgres import postgres_test_db

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token
from app.storage import storage


@pytest.fixture
def material_client(tmp_path, monkeypatch, postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    with sessions() as db:
        admin = models.User(name="Admin", email="admin@example.com", password_hash="x", role="admin")
        folder = models.Folder(name="Materials")
        db.add_all([admin, folder])
        db.commit()
        admin_id, folder_id = admin.id, folder.id

    def local_db():
        with sessions() as db:
            yield db

    monkeypatch.setattr(storage, "root", tmp_path.resolve())
    app.dependency_overrides[get_db] = local_db
    client = TestClient(app)
    try:
        yield client, {"Authorization": f"Bearer {create_token(admin_id)}"}, folder_id
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.mark.parametrize("extension,mime", [("pdf", "application/pdf"), ("mp4", "video/mp4")])
@pytest.mark.parametrize("download", [False, True])
def test_decomposed_filename_round_trips_in_content_disposition(material_client, extension, mime, download):
    client, auth, folder_id = material_client
    filename = f"qui\u0301mica.{extension}"
    payload = b"0123456789"
    uploaded = client.post(
        "/admin/materials", headers=auth, data={"title": "Test", "folder_id": str(folder_id)},
        files={"file": (filename, payload, mime)},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["filename"] == filename
    headers = dict(auth)
    if extension == "mp4":
        headers["Range"] = "bytes=2-5"
    response = client.get(f"/materials/{uploaded.json()['id']}/content", params={"download": str(download).lower()}, headers=headers)
    assert response.status_code == (206 if extension == "mp4" else 200)
    assert response.content == (b"2345" if extension == "mp4" else payload)
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(("attachment" if download else "inline") + "; ")
    assert disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == filename
