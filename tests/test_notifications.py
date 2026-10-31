import unittest
import logging
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, schemas
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class NotificationSettingsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

        def local_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = local_db
        self.client = TestClient(app)
        with self.sessions() as db:
            self.admin = models.User(name="Admin", email="admin@example.com", password_hash="unused", role="admin")
            self.student = models.User(name="Student", email="student@example.com", password_hash="unused", role="guest")
            db.add_all([self.admin, self.student])
            db.commit()
            self.admin_id, self.student_id = self.admin.id, self.student.id

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_legacy_notification_table_gets_lease_column(self):
        from app.database import ensure_notification_lease_column
        legacy_engine = create_engine("sqlite:///:memory:")
        with legacy_engine.begin() as connection:
            connection.execute(text("CREATE TABLE notification_events (id INTEGER PRIMARY KEY, logical_key VARCHAR(255), recipient VARCHAR(255), subject VARCHAR(255), body TEXT, status VARCHAR(20), attempts INTEGER, available_at DATETIME, sent_at DATETIME, last_error TEXT)"))
        with patch("app.database.engine", legacy_engine):
            ensure_notification_lease_column()
        columns = {column["name"] for column in inspect(legacy_engine).get_columns("notification_events")}
        self.assertIn("lease_expires_at", columns)
        legacy_engine.dispose()

    def headers(self, user_id):
        return {"Authorization": f"Bearer {create_token(user_id)}"}

    def test_admin_settings_default_and_patch(self):
        response = self.client.get("/admin/notification-settings", headers=self.headers(self.admin_id))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"reminder_interval_minutes": 1440})
        with self.sessions() as db:
            db.add(models.AppSetting(key="admin_recipients", value='["legacy@example.com"]')); db.commit()
        response = self.client.patch("/admin/notification-settings", headers=self.headers(self.admin_id), json={"reminder_interval_minutes": 360})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"reminder_interval_minutes": 360})
        with self.sessions() as db:
            self.assertEqual(db.get(models.AppSetting, "admin_recipients").value, '["legacy@example.com"]')
        response = self.client.patch("/admin/notification-settings", headers=self.headers(self.admin_id), json={"reminder_interval_minutes": 360, "admin_recipients": ["hacker@example.com"]})
        self.assertEqual(response.status_code, 422)

    def test_settings_reject_invalid_interval_and_non_admin(self):
        body = {"reminder_interval_minutes": 0, "admin_recipients": ["admin@example.com"]}
        response = self.client.patch("/admin/notification-settings", headers=self.headers(self.admin_id), json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/admin/notification-settings", headers=self.headers(self.student_id)).status_code, 403)

    def test_notification_settings_schema_rejects_malformed_recipients(self):
        with self.assertRaises(ValidationError):
            schemas.NotificationSettingsUpdate.model_validate({"reminder_interval_minutes": 30, "admin_recipients": ["not-an-email"]})

    def test_legacy_admin_recipient_setting_cannot_redirect_operational_notices(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)
        with self.sessions() as db:
            db.add(models.AppSetting(key="admin_recipients", value='["wrong@example.com"]'))
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(minutes=30), status="requested", note="Clase")
            db.add(booking); db.flush()
            from app.notifications import enqueue_booking_request
            enqueue_booking_request(db, booking)
            recipients = {event.recipient for event in db.scalars(select(models.NotificationEvent))}
        self.assertEqual(recipients, {"clases@beciencia.es", "student@example.com"})

    def test_booking_request_is_durable_and_deduplicated(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(minutes=30), status="requested", note="Química Bachiller")
            db.add(booking)
            db.commit()
            booking_id = booking.id
        from app.notifications import enqueue_booking_request
        with self.sessions() as db:
            enqueue_booking_request(db, db.get(models.Booking, booking_id))
            enqueue_booking_request(db, db.get(models.Booking, booking_id))
            events = list(db.scalars(select(models.NotificationEvent)))
        self.assertEqual(len(events), 2)
        self.assertEqual({event.recipient for event in events}, {"clases@beciencia.es", "student@example.com"})

    def test_log_delivery_marks_event_simulated_without_claiming_it_was_sent(self):
        from app.notifications import deliver_pending
        with self.sessions() as db:
            event = models.NotificationEvent(logical_key="test:one", recipient="student@example.com", subject="Test", body="Body")
            db.add(event)
            db.commit()
        self.assertEqual(deliver_pending(self.sessions, batch_size=10), 0)
        with self.sessions() as db:
            saved = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key == "test:one"))
        self.assertEqual(saved.status, "simulated")
        self.assertIsNone(saved.sent_at)
        self.assertEqual(deliver_pending(self.sessions, batch_size=10), 0)

    def test_confirmed_guest_reminder_targets_guest_email(self):
        from app.notifications import enqueue_due_reminders
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        with self.sessions() as db:
            db.add(models.AppSetting(key="reminder_interval_minutes", value="120"))
            db.add(models.Booking(guest_name="Guest", guest_email="guest@example.com", starts_at=now + timedelta(minutes=60),
                                  ends_at=now + timedelta(minutes=90), status="confirmed", note="Prueba"))
            db.commit()
        self.assertEqual(enqueue_due_reminders(self.sessions), 1)
        with self.sessions() as db:
            event = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like("booking:%:reminder:%")))
        self.assertEqual(event.recipient, "guest@example.com")

    def test_guest_decision_is_queued_and_future_reminders_wait_until_due(self):
        from app.notifications import enqueue_booking_decision, enqueue_due_reminders
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        with self.sessions() as db:
            guest = models.Booking(guest_name="Guest", guest_email="guest@example.com", starts_at=now + timedelta(minutes=30),
                                   ends_at=now + timedelta(minutes=90), status="declined", note="Prueba")
            confirmed = models.Booking(user_id=self.student_id, starts_at=now + timedelta(hours=26),
                                       ends_at=now + timedelta(minutes=60), status="confirmed", note="Química")
            requested = models.Booking(user_id=self.student_id, starts_at=now + timedelta(minutes=60),
                                       ends_at=now + timedelta(minutes=90), status="requested", note="Física")
            db.add_all([guest, confirmed, requested])
            db.commit()
            confirmed_id = confirmed.id
            enqueue_booking_decision(db, guest)
            db.commit()
        enqueue_due_reminders(self.sessions)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
        self.assertEqual({event.recipient for event in events}, {"guest@example.com"})
        reminders = [event for event in events if "reminder" in event.logical_key]
        self.assertEqual(reminders, [])

    def test_reminder_is_created_only_when_configured_due_time_arrives(self):
        from app.notifications import enqueue_due_reminders
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        with self.sessions() as db:
            db.add(models.AppSetting(key="reminder_interval_minutes", value="60"))
            db.add(models.Booking(user_id=self.student_id, starts_at=now + timedelta(minutes=90),
                                  ends_at=now + timedelta(minutes=120), status="confirmed"))
            db.commit()
        with patch("app.notifications.datetime") as clock:
            clock.now.return_value = now
            clock.now.side_effect = None
            self.assertEqual(enqueue_due_reminders(self.sessions), 0)
            clock.now.return_value = now + timedelta(minutes=30)
            self.assertEqual(enqueue_due_reminders(self.sessions), 1)

    def test_dry_run_log_excludes_recipient_and_message_body(self):
        from app.notifications import _send
        event = models.NotificationEvent(logical_key="x", recipient="private@example.com", subject="Private subject", body="Sensitive body")
        with self.assertLogs("app.notifications", level="INFO") as captured:
            _send(event)
        output = " ".join(captured.output)
        self.assertNotIn("private@example.com", output)
        self.assertNotIn("Sensitive body", output)

    def test_expired_processing_event_is_reclaimed(self):
        from app.notifications import deliver_pending
        with self.sessions() as db:
            event = models.NotificationEvent(logical_key="lease:expired", recipient="student@example.com",
                subject="Test", body="Body", status="processing", lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            db.add(event); db.commit()
        self.assertEqual(deliver_pending(self.sessions), 0)
        with self.sessions() as db:
            saved = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key == "lease:expired"))
        self.assertEqual(saved.status, "simulated")
        self.assertIsNone(saved.sent_at)

    def test_booking_change_cancels_pending_reminders(self):
        from app.notifications import enqueue_booking_change
        start = datetime.now(timezone.utc) + timedelta(days=2)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(minutes=30), status="confirmed")
            db.add(booking); db.flush()
            reminder = models.NotificationEvent(logical_key=f"booking:{booking.id}:reminder:{start.isoformat()}", recipient="student@example.com", subject="Reminder", body="Body")
            db.add(reminder); db.flush()
            enqueue_booking_change(db, booking, "Clase cancelada")
            self.assertEqual(reminder.status, "cancelled")
            db.rollback()

    def test_outbox_enqueue_rolls_back_with_request_transaction(self):
        from app.notifications import enqueue_contact_request
        with self.sessions() as db:
            request = models.ContactRequest(name="A", contact="a@example.com", need="help", message="x")
            db.add(request); db.flush()
            enqueue_contact_request(db, request)
            db.rollback()
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(models.ContactRequest))), [])
            self.assertEqual(list(db.scalars(select(models.NotificationEvent))), [])

    def test_smtp_ssl_uses_ssl_transport_without_starttls(self):
        from app.notifications import _send
        event = models.NotificationEvent(logical_key="x", recipient="student@example.com", subject="Hello", body="Body")
        with patch("app.notifications.settings.smtp_mode", "smtp"), patch("app.notifications.settings.smtp_host", "smtp.example.com"), \
             patch("app.notifications.settings.smtp_ssl", True), patch("app.notifications.smtplib.SMTP_SSL") as smtp_ssl, \
             patch("app.notifications.smtplib.SMTP") as smtp_plain:
            smtp_ssl.return_value.__enter__.return_value = smtp_ssl.return_value
            _send(event)
        smtp_ssl.assert_called_once()
        smtp_plain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
