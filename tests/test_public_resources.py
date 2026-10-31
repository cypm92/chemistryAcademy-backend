import unittest
from io import BytesIO
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import models
from app.database import Base, get_db
from app.database import ensure_public_resource_columns
from app.main import app
from app.security import create_token


class PublicResourcesTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        import tempfile
        self.temp_storage = tempfile.TemporaryDirectory()
        from app.storage import storage
        self.original_storage_root = storage.root
        storage.root = __import__("pathlib").Path(self.temp_storage.name)
        def local_db():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = local_db
        self.client = TestClient(app)
        with self.sessions() as db:
            admin = models.User(name="Admin", email="admin@example.com", password_hash="x", role="admin")
            student = models.User(name="Student", email="student@example.com", password_hash="x", role="guest")
            db.add_all([admin, student]); db.commit()
            self.admin_id, self.student_id = admin.id, student.id

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear(); self.engine.dispose()
        from app.storage import storage
        storage.root = self.original_storage_root
        self.temp_storage.cleanup()

    def headers(self, uid):
        return {"Authorization": f"Bearer {create_token(uid)}"}

    def test_resource_admin_routes_require_admin(self):
        self.assertEqual(self.client.get("/admin/public-resources").status_code, 401)
        self.assertEqual(self.client.get("/admin/public-resources", headers=self.headers(self.student_id)).status_code, 403)

    def test_admin_creates_public_video_and_public_list_is_sanitized(self):
        response = self.client.post("/admin/public-resources/videos", headers=self.headers(self.admin_id), json={
            "title": "Repaso", "description": "", "subject": "Química", "url": "https://www.instagram.com/reel/Abc_123/"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["url"], "https://www.instagram.com/reel/Abc_123/")
        self.assertEqual(self.client.get("/public/resources").json()["videos"][0]["url"], response.json()["url"])
        self.assertEqual(self.client.get("/admin/public-resources", headers=self.headers(self.admin_id)).status_code, 200)

    def test_instagram_rejects_non_permalink_hosts_and_paths(self):
        for url in ("https://instagram.com/accounts/login/", "https://evilinstagram.com/p/abc/", "http://instagram.com/p/abc/", "https://instagram.com:444/p/abc/"):
            response = self.client.post("/admin/public-resources/videos", headers=self.headers(self.admin_id), json={
                "title": "x", "description": "", "subject": "", "url": url})
            self.assertEqual(response.status_code, 422, f"{url}: {response.text}")

    def test_pdf_upload_public_stream_and_delete(self):
        response = self.client.post("/admin/public-resources/documents", headers=self.headers(self.admin_id), data={
            "title": "Guía", "description": "", "subject": "Química"}, files={"file": ("guide.pdf", BytesIO(b"%PDF-1.4\nbody"), "application/pdf")})
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(self.client.get(item["file_url"]).content, b"%PDF-1.4\nbody")
        self.assertEqual(self.client.get(item["file_url"]).headers["content-type"], "application/pdf")
        self.assertEqual(self.client.delete(f"/admin/public-resources/{item['id']}", headers=self.headers(self.admin_id)).status_code, 204)
        self.assertEqual(self.client.get(item["file_url"]).status_code, 404)

    def test_pdf_upload_rejects_non_pdf_and_oversized(self):
        for name, body in (("file.pdf", b"not pdf"), ("large.pdf", b"%PDF-" + b"x" * (20 * 1024 * 1024 + 1))):
            response = self.client.post("/admin/public-resources/documents", headers=self.headers(self.admin_id), data={
                "title": "x", "description": "", "subject": ""}, files={"file": (name, BytesIO(body), "application/pdf")})
            self.assertEqual(response.status_code, 413 if len(body) > 20 * 1024 * 1024 else 422, response.text)

    def test_no_video_upload_route(self):
        self.assertEqual(self.client.post("/admin/public-resources/videos/upload").status_code, 404)

    def test_exam_upload_is_classified_and_returned_in_exam_and_document_lists(self):
        response = self.client.post("/admin/public-resources/exams", headers=self.headers(self.admin_id), data={
            "title": "PAU Química", "description": "Junio", "subject": "quimica",
            "community": "Andalucía", "exam_year": "2025", "exam_part": "solutions"},
            files={"file": ("exam.pdf", BytesIO(b"%PDF-1.4\nexam"), "application/pdf")})
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual((item["category"], item["subject"], item["community"], item["exam_year"], item["exam_part"]),
                         ("exam", "quimica", "Andalucía", 2025, "solutions"))
        public = self.client.get("/public/resources").json()
        self.assertEqual(public["exams"][0]["file_url"], item["file_url"])
        self.assertEqual(public["documents"][0]["file_url"], item["file_url"])

    def test_forms_endpoint_and_documents_alias_create_form_category(self):
        response = self.client.post("/admin/public-resources/forms", headers=self.headers(self.admin_id), data={
            "title": "Formulario", "description": "", "subject": "fisica"},
            files={"file": ("form.pdf", BytesIO(b"%PDF-1.4\nform"), "application/pdf")})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["category"], "form")

        alias = self.client.post("/admin/public-resources/documents", headers=self.headers(self.admin_id), data={
            "title": "Legacy", "description": "", "subject": "matematicas"},
            files={"file": ("legacy.pdf", BytesIO(b"%PDF-1.4\nlegacy"), "application/pdf")})
        self.assertEqual(alias.status_code, 201, alias.text)
        self.assertEqual(alias.json()["category"], "form")

    def test_video_subject_is_slug_and_public_payload_includes_category(self):
        response = self.client.post("/admin/public-resources/videos", headers=self.headers(self.admin_id), json={
            "title": "Repaso", "description": "", "subject": "organica", "url": "https://www.instagram.com/p/Abc_123/"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["subject"], "organica")

    def test_new_public_resource_routes_require_admin(self):
        for route in ("/admin/public-resources/exams", "/admin/public-resources/forms"):
            response = self.client.post(route, data={"title": "x"})
            self.assertIn(response.status_code, (401, 422))

    def test_legacy_public_resource_migration_is_additive_and_idempotent(self):
        legacy = create_engine("sqlite+pysqlite:///:memory:")
        try:
            with legacy.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE public_resources (id INTEGER PRIMARY KEY, kind VARCHAR(10) NOT NULL, title VARCHAR(180) NOT NULL, description TEXT NOT NULL, subject VARCHAR(120) NOT NULL, url VARCHAR(1000), storage_key VARCHAR(255), created_at DATETIME)")
                connection.exec_driver_sql("INSERT INTO public_resources (id, kind, title, description, subject) VALUES (1, 'document', 'Old PDF', '', 'quimica')")
            ensure_public_resource_columns(legacy)
            ensure_public_resource_columns(legacy)
            with legacy.connect() as connection:
                row = connection.exec_driver_sql("SELECT category, community, exam_year, exam_part FROM public_resources WHERE id=1").one()
            self.assertEqual(tuple(row), ("form", None, None, None))
        finally:
            legacy.dispose()
