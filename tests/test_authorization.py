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

from ephemeral_postgres import postgres_test_db

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token
from sqlalchemy import select

ENGINE = None
SESSIONS = None


def _bootstrap(users, database):
    """Create a fresh PostgreSQL schema and seed `users` (list of User)."""
    global ENGINE, SESSIONS
    ENGINE = database.engine
    Base.metadata.create_all(ENGINE)
    SESSIONS = database.session_factory
    with SESSIONS() as db:
        db.add_all(users)
        db.commit()
        return {user.name: user.id for user in users}


def _local_db():
    with SESSIONS() as db:
        yield db


@pytest.fixture
def client(postgres_test_db):
    admin = models.User(name="Admin", email="admin@example.com", password_hash="x", role="admin")
    viewer = models.User(name="Viewer", email="viewer@example.com", password_hash="x", role="guest")
    ids = _bootstrap([admin, viewer], postgres_test_db)
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


# ---------------------------------------------------------------------------
# PDF Class Attachments: Download Permissions (t_d84c3a1f)
# ---------------------------------------------------------------------------

class TestPDFClassAttachments:
    """Test PDF class attachments grant can_download=True permission.
    
    Criteria from t_d84c3a1f:
    - A student can download a PDF class attachment (criterion 5).
    - A student cannot download a video class attachment (criterion 5).
    - If feasible, test the startup migration (criterion 3).
    """

    def test_pdf_class_attachment_grants_download_via_upload(self, client, ids):
        """Test POST /admin/classes/{id}/attachments with PDF sets can_download=True."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])

        # Create a confirmed booking
        with SESSIONS() as db:
            user = db.get(models.User, ids["Viewer"])
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="Test PDF attachment"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Upload PDF to the booking
        response = client_instance.post(
            f"/admin/classes/{booking_id}/attachments",
            headers=auth_admin,
            data={"title": "Test PDF"},
            files={"file": ("test.pdf", b"%PDF-1.4\ntest-content", "application/pdf")},
        )
        assert response.status_code == 200, response.text
        result = response.json()

        # Verify material was created and can_download is True in materials list
        assert len(result["materials"]) == 1
        material = result["materials"][0]
        assert material["kind"] == "pdf"
        assert material["can_download"] is True

        # Verify AccessGrant has can_download=True in database
        with SESSIONS() as db:
            grant = db.scalar(
                select(models.AccessGrant).where(
                    models.AccessGrant.user_id == ids["Viewer"],
                    models.AccessGrant.material_id == material["id"]
                )
            )
            assert grant is not None, "AccessGrant should exist"
            assert grant.can_download is True, "PDF class attachment should have can_download=True"

    def test_pdf_class_attachment_allows_student_download(self, client, ids):
        """Test that a student can download a PDF class attachment."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])
        auth_student = _auth(ids["Viewer"])

        # Create a confirmed booking
        with SESSIONS() as db:
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="PDF download test"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Upload PDF to the booking
        response = client_instance.post(
            f"/admin/classes/{booking_id}/attachments",
            headers=auth_admin,
            data={"title": "Test PDF"},
            files={"file": ("test.pdf", b"%PDF-1.4\ntest-pdf-content", "application/pdf")},
        )
        assert response.status_code == 200, response.text
        material_id = response.json()["materials"][0]["id"]

        # Student should be able to download it
        response = client_instance.get(
            f"/materials/{material_id}/content",
            headers=auth_student,
            params={"download": "true"}
        )
        assert response.status_code == 200, f"Student should be able to download PDF: {response.text}"
        assert response.content == b"%PDF-1.4\ntest-pdf-content"

    def test_video_class_attachment_denies_student_download(self, client, ids):
        """Test that a student cannot download a video class attachment."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])
        auth_student = _auth(ids["Viewer"])

        # Create a confirmed booking
        with SESSIONS() as db:
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="Video attachment test"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Upload video to the booking
        response = client_instance.post(
            f"/admin/classes/{booking_id}/attachments",
            headers=auth_admin,
            data={"title": "Test Video"},
            files={"file": ("test.mp4", b"fake-video-content", "video/mp4")},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        material = result["materials"][0]
        assert material["kind"] == "video"
        assert material["can_download"] is False
        material_id = material["id"]

        # Student should NOT be able to download it
        response = client_instance.get(
            f"/materials/{material_id}/content",
            headers=auth_student,
            params={"download": "true"}
        )
        assert response.status_code == 403, "Student should not be able to download video class attachment"

    def test_pdf_via_attach_material_grants_download(self, client, ids):
        """Test POST /admin/classes/{id}/materials with PDF sets can_download=True."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])

        # Create folder and upload a PDF material
        folder_id = _get_or_create_folder(client, ids, "PDF Materials")
        material_id = _upload_material(client, ids, title="Shared PDF", folder_id=folder_id, auth=auth_admin)

        # Create a confirmed booking
        with SESSIONS() as db:
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="Material attachment test"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Attach the PDF material to the booking
        response = client_instance.post(
            f"/admin/classes/{booking_id}/materials",
            headers=auth_admin,
            json={"material_id": material_id}
        )
        assert response.status_code == 200, response.text
        result = response.json()

        # Verify can_download is True in materials list
        assert len(result["materials"]) == 1
        material = result["materials"][0]
        assert material["kind"] == "pdf"
        assert material["can_download"] is True

        # Verify AccessGrant has can_download=True in database
        with SESSIONS() as db:
            grant = db.scalar(
                select(models.AccessGrant).where(
                    models.AccessGrant.user_id == ids["Viewer"],
                    models.AccessGrant.material_id == material_id
                )
            )
            assert grant is not None, "AccessGrant should exist"
            assert grant.can_download is True, "PDF attachment via attach_material should have can_download=True"

    def test_upgrading_grant_does_not_downgrade_download(self, client, ids):
        """Test that re-attaching a PDF doesn't downgrade can_download (upgrade-only pattern)."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])

        # Create folder and upload a PDF material
        folder_id = _get_or_create_folder(client, ids, "Upgrade Test")
        material_id = _upload_material(client, ids, title="Upgrade PDF", folder_id=folder_id, auth=auth_admin)

        # Create a confirmed booking
        with SESSIONS() as db:
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="Upgrade test"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Attach the PDF material the first time
        response = client_instance.post(
            f"/admin/classes/{booking_id}/materials",
            headers=auth_admin,
            json={"material_id": material_id}
        )
        assert response.status_code == 200, response.text

        # Verify AccessGrant has can_download=True
        with SESSIONS() as db:
            grant = db.scalar(
                select(models.AccessGrant).where(
                    models.AccessGrant.user_id == ids["Viewer"],
                    models.AccessGrant.material_id == material_id
                )
            )
            assert grant.can_download is True

        # Remove the material link to test re-attachment
        with SESSIONS() as db:
            link = db.scalar(
                select(models.BookingMaterial).where(
                    models.BookingMaterial.booking_id == booking_id,
                    models.BookingMaterial.material_id == material_id
                )
            )
            if link:
                db.delete(link)
                db.commit()

        # Attach again - should maintain can_download=True
        response = client_instance.post(
            f"/admin/classes/{booking_id}/materials",
            headers=auth_admin,
            json={"material_id": material_id}
        )
        assert response.status_code == 200, response.text

        # Verify AccessGrant still has can_download=True
        with SESSIONS() as db:
            grant = db.scalar(
                select(models.AccessGrant).where(
                    models.AccessGrant.user_id == ids["Viewer"],
                    models.AccessGrant.material_id == material_id
                )
            )
            assert grant.can_download is True, "can_download should remain True on re-attachment"

    def test_migration_fixes_existing_pdf_attachments(self, client, ids):
        """Test that ensure_pdf_attachment_download_permission() fixes existing grants."""
        from datetime import datetime, timedelta, timezone
        from app.main import ensure_pdf_attachment_download_permission

        # Create a PDF class attachment manually with can_download=False
        with SESSIONS() as db:
            folder = models.Folder(name="Migration Test")
            db.add(folder)
            db.flush()

            material = models.Material(
                title="Old PDF Attachment",
                folder_id=folder.id,
                kind="pdf",
                filename="old.pdf",
                storage_key="old-pdf-key",
                content_type="application/pdf",
                size_bytes=100,
                is_class_attachment=True
            )
            db.add(material)
            db.flush()

            # Create a grant with can_download=False (the broken state)
            grant = models.AccessGrant(
                user_id=ids["Viewer"],
                material_id=material.id,
                starts_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=365),
                can_download=False  # Broken state before the fix
            )
            db.add(grant)
            db.commit()
            grant_id = grant.id

        # Run the migration
        ensure_pdf_attachment_download_permission()

        # Verify the grant was fixed
        with SESSIONS() as db:
            grant = db.get(models.AccessGrant, grant_id)
            assert grant.can_download is True, "Migration should fix PDF attachment grants"

    def test_class_data_includes_can_download_field(self, client, ids):
        """Test that class_data() response includes can_download field for each material."""
        from datetime import datetime, timedelta, timezone
        client_instance = client["client"]
        auth_admin = _auth(ids["Admin"])

        # Create a confirmed booking
        with SESSIONS() as db:
            booking = models.Booking(
                user_id=ids["Viewer"],
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="confirmed",
                note="class_data test"
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id

        # Upload a PDF
        response = client_instance.post(
            f"/admin/classes/{booking_id}/attachments",
            headers=auth_admin,
            data={"title": "Test PDF"},
            files={"file": ("test.pdf", b"%PDF-1.4\ntest", "application/pdf")},
        )
        assert response.status_code == 200, response.text
        result = response.json()

        # Upload a video
        response = client_instance.post(
            f"/admin/classes/{booking_id}/attachments",
            headers=auth_admin,
            data={"title": "Test Video"},
            files={"file": ("test.mp4", b"fake-video", "video/mp4")},
        )
        assert response.status_code == 200, response.text
        result = response.json()

        # Verify both materials have can_download field set correctly
        assert len(result["materials"]) == 2
        pdf_material = next((m for m in result["materials"] if m["kind"] == "pdf"), None)
        video_material = next((m for m in result["materials"] if m["kind"] == "video"), None)

        assert pdf_material is not None, "PDF material should exist"
        assert video_material is not None, "Video material should exist"
        assert "can_download" in pdf_material, "can_download field should exist for PDF"
        assert "can_download" in video_material, "can_download field should exist for video"
        assert pdf_material["can_download"] is True, "PDF should have can_download=True"
        assert video_material["can_download"] is False, "Video should have can_download=False"
