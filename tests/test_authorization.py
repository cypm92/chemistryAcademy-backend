"""Authorization behavior suite.

Covers the permission scenarios agreed for the material layer and the admin
surface, verified against the running application (no schema migration, no
viewer or file movement):

* Anonymous requests to a protected endpoint are rejected with 401.
* A non-granted user (or an expired grant) on a material is rejected with 403.
* A grant with `can_download=False` rejects downloads with 403.
* An active grant (or admin) grants access: 200 on GET content, 206 on a range.
* Inactive users are rejected by `current_user` with 401.
* Admin endpoints reject anonymous (401) and non-admin (403) callers.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token

ENGINE = None
SESSIONS = None


def _bootstrap(users):
    """Create a fresh in-memory schema and seed `users` (list of User)."""
    global ENGINE, SESSIONS
    ENGINE = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(ENGINE)
    SESSIONS = sessionmaker(bind=ENGINE)
    with SESSIONS() as db:
        db.add_all(users)
        db.commit()
        return {user.name: user.id for user in users}


def _local_db():
    with SESSIONS() as db:
        yield db


@pytest.fixture
def client():
    admin = models.User(name="Admin", email="admin@example.com", password_hash="x", role="admin")
    viewer = models.User(name="Viewer", email="viewer@example.com", password_hash="x", role="guest")
    ids = _bootstrap([admin, viewer])
    app.dependency_overrides[get_db] = _local_db
    with TestClient(app) as c:
        yield {"client": c, "ids": ids}
    app.dependency_overrides.pop(get_db, None)
    ENGINE.dispose()


def _auth(user_id):
    return {"Authorization": f"Bearer {create_token(user_id)}"}


def _get_or_create_folder(client, ids, name="Materials"):
    client_instance = client["client"]
    auth = _auth(ids["Admin"])
    response = client_instance.get("/admin/folders", headers=auth)
    if response.status_code == 200:
        for folder in response.json():
            if folder["name"] == name:
                return folder["id"]
    response = client_instance.post("/admin/folders", headers=auth, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload_material(client, ids, title="Material", folder_id=None, auth=None):
    client_instance = client["client"]
    auth = auth or _auth(ids["Admin"])
    if folder_id is None:
        folder_id = _get_or_create_folder(client, ids)
    response = client_instance.post(
        "/admin/materials",
        headers=auth,
        data={"title": title, "folder_id": str(folder_id)},
        files={"file": ("sample.pdf", b"%PDF-1.4\nplaceholder-pdf", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# Authentication / invalid callers (current_user -> 401)
# ---------------------------------------------------------------------------

class TestCurrentUser:
    def test_missing_token_returns_401(self, client):
        client_instance = client["client"]
        response = client_instance.get("/materials/1/content")
        assert response.status_code == 401

    def test_malformed_token_returns_401(self, client):
        client_instance = client["client"]
        response = client_instance.get(
            "/materials/1/content", headers={"Authorization": "Bearer not-a-jwt!!!"}
        )
        assert response.status_code == 401

    def test_invalid_signature_returns_401(self, client):
        client_instance = client["client"]
        response = client_instance.get(
            "/materials/1/content", headers={"Authorization": "Bearer abc.def.ghi"}
        )
        assert response.status_code == 401

    def test_non_existent_user_returns_401(self, client):
        client_instance = client["client"]
        response = client_instance.get(
            "/materials/1/content", headers={"Authorization": "Bearer " + create_token(99999)}
        )
        assert response.status_code == 401

    def test_inactive_user_returns_401(self, client):
        client_instance = client["client"]
        ids = client["ids"]
        with SESSIONS() as db:
            target = db.get(models.User, ids["Viewer"])
            target.is_active = False
            db.commit()
        response = client_instance.get("/materials/1/content", headers=_auth(ids["Viewer"]))
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Admin endpoints: anonymous -> 401, non-admin -> 403
# ---------------------------------------------------------------------------

class TestAdminEndpoints:
    """Admin-protected routes on the running application.

    GET routes are checked with GET; the POST-only grants routes are also
    checked so the authorization matrix is complete for every method the
    endpoint accepts.
    """
    GET_ADMIN_ROUTES = [
        "/admin/classes",
        "/admin/users",
        "/admin/materials",
        "/admin/folders",
        "/admin/tags",
        "/admin/public-resources",
        "/admin/notification-settings",
    ]
    POST_ADMIN_ROUTES = [
        "/admin/grants",
        "/admin/tag-grants",
    ]

    def test_anonymous_admin_route_is_401(self, client):
        client_instance = client["client"]
        for path in self.GET_ADMIN_ROUTES:
            assert client_instance.get(path).status_code == 401, path
        for path in self.POST_ADMIN_ROUTES:
            assert client_instance.post(path).status_code == 401, path

    @pytest.mark.parametrize("path", GET_ADMIN_ROUTES + POST_ADMIN_ROUTES)
    def test_non_admin_on_admin_route_is_403(self, client, path):
        client_instance = client["client"]
        ids = client["ids"]
        if path in self.GET_ADMIN_ROUTES:
            response = client_instance.get(path, headers=_auth(ids["Viewer"]))
        else:
            response = client_instance.post(path, headers=_auth(ids["Viewer"]))
        assert response.status_code == 403, path

    def test_admin_on_own_route_is_200(self, client):
        client_instance = client["client"]
        ids = client["ids"]
        for path in self.GET_ADMIN_ROUTES:
            response = client_instance.get(path, headers=_auth(ids["Admin"]))
            assert response.status_code == 200, (path, response.text)


# ---------------------------------------------------------------------------
# Material content: the permission scenarios for the material layer
# ---------------------------------------------------------------------------

@pytest.fixture
def ids(client):
    return client["ids"]


class TestMaterialPermissions:
    @pytest.fixture
    def material(self, client):
        return _upload_material(client, client["ids"])

    @pytest.fixture
    def folder(self, client):
        client_instance = client["client"]
        response = client_instance.post(
            "/admin/folders", headers=_auth(client["ids"]["Admin"]), data={"name": "Materials"}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def test_no_token_is_401(self, client, material):
        client_instance = client["client"]
        response = client_instance.get(f"/materials/{material}/content")
        assert response.status_code == 401

    def test_ungranted_viewer_is_403(self, client, material, ids):
        client_instance = client["client"]
        response = client_instance.get(
            f"/materials/{material}/content", headers=_auth(ids["Viewer"])
        )
        assert response.status_code == 403

    def test_expired_grant_is_403(self, client, material, ids):
        from datetime import datetime, timedelta, timezone
        with SESSIONS() as db:
            db.add(
                models.AccessGrant(
                    user_id=ids["Viewer"],
                    material_id=material,
                    starts_at=datetime.now(timezone.utc) - timedelta(days=1),
                    expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                    can_download=True,
                )
            )
            db.commit()
        client_instance = client["client"]
        response = client_instance.get(
            f"/materials/{material}/content", headers=_auth(ids["Viewer"])
        )
        assert response.status_code == 403

    def test_download_disabled_is_403(self, client, material, ids):
        from datetime import datetime, timedelta, timezone
        with SESSIONS() as db:
            db.add(
                models.AccessGrant(
                    user_id=ids["Viewer"],
                    material_id=material,
                    starts_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    can_download=False,
                )
            )
            db.commit()
        client_instance = client["client"]
        response = client_instance.get(
            f"/materials/{material}/content",
            headers=_auth(ids["Viewer"]),
            params={"download": "true"},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "extension,mime,expected", [("pdf", "application/pdf", 200), ("mp4", "video/mp4", 206)]
    )
    def test_active_grant_grants_access(self, client, material, ids, extension, mime, expected):
        from datetime import datetime, timedelta, timezone
        with SESSIONS() as db:
            db.add(
                models.AccessGrant(
                    user_id=ids["Viewer"],
                    material_id=material,
                    starts_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    can_download=True,
                )
            )
            db.commit()
        client_instance = client["client"]
        headers = _auth(ids["Viewer"])
        if extension == "mp4":
            headers["Range"] = "bytes=2-5"
        response = client_instance.get(
            f"/materials/{material}/content", headers=headers, params={"download": "false"}
        )
        expected = 206 if extension == "mp4" else 200
        assert response.status_code == expected, response.text
        if extension == "mp4":
            assert response.headers["Content-Range"].startswith("bytes 2-5")

    def test_admin_bypasses_grant_check(self, client, material, ids):
        client_instance = client["client"]
        response = client_instance.get(f"/materials/{material}/content", headers=_auth(ids["Admin"]))
        assert response.status_code == 200, response.text

    def test_viewer_with_active_grant_sees_content(self, client, material, ids):
        from datetime import datetime, timedelta, timezone
        with SESSIONS() as db:
            db.add(
                models.AccessGrant(
                    user_id=ids["Viewer"],
                    material_id=material,
                    starts_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    can_download=True,
                )
            )
            db.commit()
        client_instance = client["client"]
        response = client_instance.get(f"/materials/{material}/content", headers=_auth(ids["Viewer"]))
        assert response.status_code == 200, response.text
        assert response.content == b"%PDF-1.4\nplaceholder-pdf"

    def test_admin_grants_list_returns_200(self, client, material, ids):
        client_instance = client["client"]
        response = client_instance.get(
            f"/admin/users/{ids['Viewer']}/grants", headers=_auth(ids["Admin"])
        )
        assert response.status_code == 200, response.text
