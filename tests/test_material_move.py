import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class MaterialMoveTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

        def local_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = local_db
        self.client = TestClient(app)
        with self.sessions() as db:
            admin = models.User(name="Admin", email="admin@example.com", password_hash="unused", role="admin")
            student = models.User(name="Alumno", email="student@example.com", password_hash="unused", role="guest")
            source = models.Folder(name="Origen")
            destination = models.Folder(name="Destino")
            db.add_all([admin, student, source, destination])
            db.flush()
            material = models.Material(
                title="Apuntes", description="Ejercicios", folder_id=source.id,
                kind="pdf", filename="apuntes.pdf", storage_key="same-file.pdf",
                content_type="application/pdf", size_bytes=123,
            )
            db.add(material)
            db.flush()
            grant = models.AccessGrant(
                user_id=student.id, material_id=material.id,
                starts_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            db.add(grant)
            db.commit()
            self.admin_id = admin.id
            self.student_id = student.id
            self.source_id = source.id
            self.destination_id = destination.id
            self.material_id = material.id
            self.grant_id = grant.id

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def move(self, user_id, folder_id):
        return self.client.patch(
            f"/admin/materials/{self.material_id}/folder",
            json={"folder_id": folder_id},
            headers={"Authorization": f"Bearer {create_token(user_id)}"},
        )

    def test_admin_moves_material_without_replacing_file_or_grant(self):
        response = self.move(self.admin_id, self.destination_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["folder"]["path"], "Destino")
        with self.sessions() as db:
            material = db.get(models.Material, self.material_id)
            self.assertEqual(material.folder_id, self.destination_id)
            self.assertEqual(material.storage_key, "same-file.pdf")
            grant = db.scalar(select(models.AccessGrant).where(models.AccessGrant.id == self.grant_id))
            self.assertEqual(grant.material_id, self.material_id)

    def test_student_cannot_move_material(self):
        response = self.move(self.student_id, self.destination_id)
        self.assertEqual(response.status_code, 403)
        with self.sessions() as db:
            self.assertEqual(db.get(models.Material, self.material_id).folder_id, self.source_id)

    def test_missing_destination_does_not_move_material(self):
        response = self.move(self.admin_id, 9999)
        self.assertEqual(response.status_code, 404)
        with self.sessions() as db:
            self.assertEqual(db.get(models.Material, self.material_id).folder_id, self.source_id)


if __name__ == "__main__":
    unittest.main()
