import unittest
import logging
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker
from ephemeral_postgres import ephemeral_postgres

from app import models, schemas
from app.database import Base, get_db
from app.main import app
from app.security import create_token


class NotificationSettingsTest(unittest.TestCase):
    def setUp(self):
        context = ephemeral_postgres()
        database = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.engine = database.engine
        self.assertEqual(self.engine.dialect.name, 'postgresql')
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

    def test_deleted_booking_delivers_cancellation_but_not_confirmation(self):
        from app.notifications import enqueue_booking_change, deliver_pending
        with self.sessions() as db:
            start = datetime(2030, 1, 1, tzinfo=timezone.utc)
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status='cancelled')
            db.add(booking); db.flush()
            enqueue_booking_change(db, booking, 'Clase cancelada')
            db.add(models.NotificationEvent(logical_key=f'booking:{booking.id}:confirmed:student', recipient='x@example.com', subject='confirmed', body='x'))
            db.commit(); db.delete(booking); db.commit()
        with patch('app.notifications._send', return_value=True) as send:
            deliver_pending(self.sessions)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
            self.assertTrue(all(e.status == ('sent' if e.subject == 'Clase cancelada' else 'cancelled') for e in events))
            self.assertEqual(send.call_count, sum(e.subject == 'Clase cancelada' for e in events))
            self.assertGreater(send.call_count, 0)

    def test_legacy_notification_table_gets_lease_column(self):
        from app.database import ensure_notification_lease_column
        context = ephemeral_postgres()
        legacy_engine = context.__enter__().engine
        self.addCleanup(context.__exit__, None, None, None)
        with legacy_engine.begin() as connection:
            connection.execute(text("CREATE TABLE notification_events (id INTEGER PRIMARY KEY, logical_key VARCHAR(255), recipient VARCHAR(255), subject VARCHAR(255), body TEXT, status VARCHAR(20), attempts INTEGER, available_at TIMESTAMP, sent_at TIMESTAMP, last_error TEXT)"))
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

    def test_booking_notifications_label_madrid_time_across_dst(self):
        from app.notifications import enqueue_booking_request, enqueue_booking_decision, enqueue_booking_change, enqueue_due_reminders
        start = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
        with self.sessions() as db:
            booking = models.Booking(guest_name="Guest", guest_email="guest@example.com", starts_at=start,
                                     ends_at=end, status="confirmed", note="Prueba")
            db.add(booking); db.flush()
            enqueue_booking_request(db, booking)
            enqueue_booking_decision(db, booking)
            enqueue_booking_change(db, booking, "Clase reprogramada", start - timedelta(days=1))
            db.commit()
        with patch("app.notifications.datetime") as clock:
            clock.now.return_value = start - timedelta(minutes=30)
            self.assertEqual(enqueue_due_reminders(self.sessions), 1)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
            self.assertEqual(len(events), 6)
            for event in events:
                self.assertIn("domingo 25 de octubre de 2026 a las 02:30 (UTC+02:00)", event.body)
                self.assertIn("domingo 25 de octubre de 2026 a las 02:30 (UTC+01:00)", event.body)
                self.assertIn("hora de Madrid", event.body)
            changes = [event for event in events if event.subject == "Clase reprogramada"]
            self.assertEqual(len(changes), 2)
            for event in changes:
                self.assertIn("sábado 24 de octubre de 2026, a las 02:30 (hora de Madrid)", event.body)

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

    def test_cancellation_invalidates_only_unsent_related_booking_events(self):
        from app.notifications import enqueue_booking_change
        start = datetime.now(timezone.utc) + timedelta(days=3)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status="cancelled")
            db.add(booking); db.flush()
            prefix = f"booking:{booking.id}:"
            for key, status in [("requested:student", "pending"), ("confirmed:student", "processing"),
                                ("reminder:old", "pending"), ("Clase reprogramada:old:student", "pending"),
                                ("declined:student", "sent"), ("requested:admin:x", "simulated")]:
                db.add(models.NotificationEvent(logical_key=prefix + key, recipient="x@example.com", subject="x", body="x", status=status))
            db.add(models.NotificationEvent(logical_key="contact:1:admin:x", recipient="x@example.com", subject="x", body="x"))
            db.flush()
            enqueue_booking_change(db, booking, "Clase cancelada")
            db.commit()
            statuses = {e.logical_key: e.status for e in db.scalars(select(models.NotificationEvent))}
            for suffix in ("requested:student", "confirmed:student", "reminder:old", "Clase reprogramada:old:student"):
                self.assertEqual(statuses[prefix + suffix], "cancelled")
            self.assertEqual(statuses[prefix + "declined:student"], "sent")
            self.assertEqual(statuses[prefix + "requested:admin:x"], "simulated")
            self.assertEqual(statuses["contact:1:admin:x"], "pending")
            self.assertEqual(sum(v == "pending" for k, v in statuses.items() if k.startswith(prefix + "Clase cancelada:")), 2)

    def test_delivery_cancels_reminders_at_or_after_class_start(self):
        from app.notifications import deliver_pending
        now = datetime.now(timezone.utc) + timedelta(minutes=2)
        with self.sessions() as db:
            for offset in (-1, 0, 1):
                start = now + timedelta(seconds=offset)
                booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status="confirmed")
                db.add(booking); db.flush()
                db.add(models.NotificationEvent(logical_key=f"booking:{booking.id}:reminder:{start.isoformat()}",
                    recipient="x@example.com", subject=str(offset), body="x", available_at=now - timedelta(minutes=1)))
            db.commit()
        with patch("app.notifications.datetime", wraps=datetime) as clock, patch("app.notifications._send", return_value=True) as send:
            clock.now.return_value = now
            self.assertEqual(deliver_pending(self.sessions), 1)
            send.assert_called_once()
        with self.sessions() as db:
            statuses = {e.subject: e.status for e in db.scalars(select(models.NotificationEvent))}
            self.assertEqual(statuses, {"-1": "cancelled", "0": "cancelled", "1": "sent"})

    def test_delivery_skips_legacy_confirmation_for_cancelled_booking(self):
        from app.notifications import deliver_pending
        start = datetime.now(timezone.utc) + timedelta(days=3)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status="cancelled")
            db.add(booking); db.flush()
            db.add(models.NotificationEvent(logical_key=f"booking:{booking.id}:confirmed:student", recipient="x@example.com", subject="x", body="x"))
            db.commit()
        with patch("app.notifications._send") as send:
            deliver_pending(self.sessions)
            send.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(models.NotificationEvent)).status, "cancelled")

    def test_cancel_during_send_does_not_resurrect_event_after_failure(self):
        from app.notifications import deliver_pending
        with self.sessions() as db:
            event = models.NotificationEvent(logical_key="unrelated:race", recipient="x@example.com", subject="x", body="x")
            db.add(event); db.commit(); event_id = event.id
        def cancel_then_fail(_):
            with self.sessions() as other:
                saved = other.get(models.NotificationEvent, event_id)
                saved.status, saved.lease_expires_at = "cancelled", None
                other.commit()
            raise RuntimeError("simulated failure")
        with patch("app.notifications._send", side_effect=cancel_then_fail):
            deliver_pending(self.sessions)
        with self.sessions() as db:
            self.assertEqual(db.get(models.NotificationEvent, event_id).status, "cancelled")

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

    def test_requested_edit_replaces_stale_requests(self):
        from app.notifications import enqueue_booking_request
        start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status="requested", note="Química")
            db.add(booking); db.flush(); enqueue_booking_request(db, booking); db.commit(); booking_id = booking.id
        response = self.client.patch(f"/admin/classes/{booking_id}", headers=self.headers(self.admin_id), json={"starts_at": (start + timedelta(hours=2)).isoformat(), "duration_slots": 1, "subject": "Física ESO", "admin_comment": ""})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
            active = [e for e in events if e.status == "pending"]
            self.assertEqual(len(active), 2)
            self.assertTrue(all("Física" in e.body for e in active))
            self.assertEqual(sum(e.status == "cancelled" for e in events), 2)

    def test_repeated_edits_and_reminders_have_distinct_generations(self):
        from app.notifications import enqueue_booking_change, enqueue_due_reminders
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        with self.sessions() as db:
            booking = models.Booking(user_id=self.student_id, starts_at=start, ends_at=start + timedelta(hours=1), status="confirmed", note="Química")
            db.add(booking); db.commit(); booking_id = booking.id
        enqueue_due_reminders(self.sessions)
        for offset, duration, subject in [(1, 1, "Física"), (0, 2, "Química"), (0, 1, "Física")]:
            with self.sessions() as db:
                booking = db.get(models.Booking, booking_id); previous = booking.starts_at
                booking.starts_at = start + timedelta(hours=offset); booking.ends_at = booking.starts_at + timedelta(hours=duration); booking.note = subject
                enqueue_booking_change(db, booking, "Clase reprogramada", previous); db.commit()
            enqueue_due_reminders(self.sessions)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent)))
            changes = [e for e in events if e.subject == "Clase reprogramada"]
            reminders = [e for e in events if e.subject == "Recordatorio de tu clase"]
            self.assertEqual(len(changes), 6)
            self.assertEqual(len(reminders), 4)
            self.assertEqual(sum(e.status == "pending" for e in changes), 2)
            self.assertEqual(sum(e.status == "pending" for e in reminders), 1)
            self.assertIn("Física", [e for e in reminders if e.status == "pending"][0].body)

    def test_success_after_cancellation_preserves_cancellation_and_records_send(self):
        from app.notifications import deliver_pending
        with self.sessions() as db:
            event = models.NotificationEvent(logical_key="unrelated:race:sent", recipient="x@example.com", subject="x", body="x")
            db.add(event); db.commit(); event_id = event.id
        def cancel_then_send(_):
            with self.sessions() as other:
                saved = other.get(models.NotificationEvent, event_id)
                saved.status, saved.lease_expires_at = "cancelled", None
                other.commit()
            return True
        with patch("app.notifications._send", side_effect=cancel_then_send):
            self.assertEqual(deliver_pending(self.sessions), 1)
        with self.sessions() as db:
            saved = db.get(models.NotificationEvent, event_id)
            self.assertEqual(saved.status, "cancelled")
            self.assertIsNotNone(saved.sent_at)

    def test_smtp_sends_safe_html_and_plain_text_for_legacy_events(self):
        from app.notifications import _send
        body = 'Pendiente de confirmación\n<script>alert("x")</script> & detalles'
        event = models.NotificationEvent(logical_key="old:1", recipient="student@example.com", subject='<img src=x onerror="alert(1)">', body=body)
        with patch("app.notifications.settings.smtp_mode", "smtp"), patch("app.notifications.settings.smtp_host", "smtp.example.com"), \
             patch("app.notifications.settings.smtp_ssl", True), patch("app.notifications.smtplib.SMTP_SSL") as transport:
            smtp = transport.return_value.__enter__.return_value
            self.assertTrue(_send(event))
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().rstrip("\n"), body)
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('<img src=x', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

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
