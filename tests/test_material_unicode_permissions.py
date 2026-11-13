from datetime import datetime, timedelta, timezone
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
def material_env(tmp_path, monkeypatch, postgres_test_db):
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory
    with sessions() as db:
        users = [models.User(name=role, email=f"{role}@example.com", password_hash="x", role="admin" if role == "admin" else "guest") for role in ("admin", "reader", "downloader", "expired", "stranger")]
        folder = models.Folder(name="Materials")
        db.add_all([*users, folder])
        db.commit()
        ids = {user.name: user.id for user in users}
        folder_id = folder.id

    def local_db():
        with sessions() as db:
            yield db

    monkeypatch.setattr(storage, "root", tmp_path.resolve())
    monkeypatch.setitem(app.dependency_overrides, get_db, local_db)
    with TestClient(app) as client:
        yield client, sessions, ids, folder_id
    engine.dispose()


def auth(user_id):
    return {"Authorization": f"Bearer {create_token(user_id)}"}


def upload(env, filename, mime, expected_filename=None):
    client, _, ids, folder_id = env
    response = client.post("/admin/materials", headers=auth(ids["admin"]), data={"title": "Test", "folder_id": str(folder_id)}, files={"file": (filename, b"0123456789", mime)})
    assert response.status_code == 201, response.text
    assert response.json()["filename"] == (expected_filename if expected_filename is not None else filename)
    return response.json()["id"]


@pytest.mark.parametrize("stem,received_stem", [
    ("quimica", "quimica"), ("química", "química"),
    ("qui\u0301mica", "qui\u0301mica"), ("año", "año"),
    ('notas "uno"', "notas %22uno%22"), ("notas %22uno%22", "notas %22uno%22"),
])
@pytest.mark.parametrize("extension,mime", [("pdf", "application/pdf"), ("mp4", "video/mp4")])
@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_uploaded_name_and_bytes_survive_content_response(material_env, stem, received_stem, extension, mime, download, partial):
    client, _, ids, _ = material_env
    filename = f"{stem}.{extension}"
    # HTTPX multipart sends quotes as %22; literal %22 is indistinguishable.
    received_filename = f"{received_stem}.{extension}"
    material_id = upload(material_env, filename, mime, expected_filename=received_filename)
    headers = auth(ids["admin"])
    if partial:
        headers["Range"] = "bytes=2-5"
    response = client.get(f"/materials/{material_id}/content", params={"download": str(download).lower()}, headers=headers)
    # Video ranges are handled by the endpoint; PDF ranges by the installed Starlette FileResponse.
    assert response.status_code == (206 if partial else 200)
    assert response.content == (b"2345" if partial else b"0123456789")
    if partial:
        assert response.headers["content-range"] == "bytes 2-5/10"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(("attachment" if download else "inline") + "; filename*=UTF-8''")
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == received_filename
    assert '"' not in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("extension,mime", [("pdf", "application/pdf"), ("mp4", "video/mp4")])
@pytest.mark.parametrize("partial", [False, True])
def test_material_permissions_for_unicode_names(material_env, extension, mime, partial):
    client, sessions, ids, _ = material_env
    material_id = upload(material_env, f"año qui\u0301mica.{extension}", mime)
    now = datetime.now(timezone.utc)
    with sessions() as db:
        for role, can_download, start, end in [
            ("reader", False, now - timedelta(days=1), now + timedelta(days=1)),
            ("downloader", True, now - timedelta(days=1), now + timedelta(days=1)),
            ("expired", True, now - timedelta(days=2), now - timedelta(days=1)),
        ]:
            db.add(models.AccessGrant(user_id=ids[role], material_id=material_id, can_download=can_download, starts_at=start, expires_at=end))
        db.commit()
    url = f"/materials/{material_id}/content"
    for role, download, expected in [(None, False, 401), ("stranger", False, 403), ("expired", False, 403), ("reader", False, 206 if partial else 200), ("reader", True, 403), ("downloader", True, 206 if partial else 200), ("admin", True, 206 if partial else 200)]:
        headers = auth(ids[role]) if role else {}
        if partial:
            headers["Range"] = "bytes=2-5"
        response = client.get(url, headers=headers, params={"download": str(download).lower()})
        assert response.status_code == expected, (role, download, response.text)
        if expected in (200, 206):
            assert response.content == (b"2345" if partial else b"0123456789")
        else:
            assert "content-disposition" not in response.headers
            assert b"0123456789" not in response.content


@pytest.mark.parametrize("extension,mime", [("pdf", "application/pdf"), ("mp4", "video/mp4")])
@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_stored_quotes_are_safely_encoded_in_response(material_env, extension, mime, download, partial):
    client, sessions, ids, _ = material_env
    material_id = upload(material_env, f"safe.{extension}", mime)
    filename = f'notas "uno".{extension}'
    with sessions() as db:
        db.get(models.Material, material_id).filename = filename
        db.commit()
    headers = auth(ids["admin"])
    if partial:
        headers["Range"] = "bytes=2-5"
    response = client.get(f"/materials/{material_id}/content", headers=headers, params={"download": str(download).lower()})
    assert response.status_code == (206 if partial else 200)
    assert response.content == (b"2345" if partial else b"0123456789")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(("attachment" if download else "inline") + "; filename*=UTF-8''")
    assert '"' not in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == filename
