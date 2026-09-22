import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import settings
from app.database import Base
from app.main import seed_admin
from app.security import hash_password, verify_password


class AdminBootstrapTest(unittest.TestCase):
    def test_restart_preserves_existing_admin_password(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)

        with patch("app.main.SessionLocal", sessions):
            seed_admin()
            with sessions() as db:
                user = db.scalar(select(models.User).where(
                    models.User.email == settings.admin_email.lower()
                ))
                self.assertIsNotNone(user)
                user.password_hash = hash_password("changed-by-administrator")
                db.commit()

            seed_admin()
            with sessions() as db:
                user = db.scalar(select(models.User).where(
                    models.User.email == settings.admin_email.lower()
                ))
                self.assertTrue(verify_password("changed-by-administrator", user.password_hash))
                self.assertEqual(user.role, "admin")
                self.assertEqual(db.query(models.User).count(), 1)


if __name__ == "__main__":
    unittest.main()
