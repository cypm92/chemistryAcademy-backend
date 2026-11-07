import unittest
from datetime import datetime, timezone


class ReportScheduleTest(unittest.TestCase):
    def test_only_current_madrid_day_after_ten_is_due(self):
        from app.admin_reports import due_periods
        self.assertEqual(due_periods(datetime(2026, 3, 29, 7, 59, tzinfo=timezone.utc)), (None, '2026-02'))
        self.assertEqual(due_periods(datetime(2026, 3, 29, 8, 0, tzinfo=timezone.utc)), ('2026-03-29', '2026-02'))
        self.assertEqual(due_periods(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)), ('2026-01-01', '2025-12'))
        self.assertEqual(due_periods(datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)), ('2026-01-15', '2025-12'))
        self.assertEqual(due_periods(datetime(2026, 4, 1, 7, 59, tzinfo=timezone.utc)), (None, None))
        self.assertEqual(due_periods(datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)), ('2026-04-01', '2026-03'))
        self.assertEqual(due_periods(datetime(2026, 10, 25, 8, 59, tzinfo=timezone.utc))[0], None)
        self.assertEqual(due_periods(datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc))[0], '2026-10-25')


class ReportsTest(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app import models  # Register mappings before creating the isolated schema.
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_existing_reports_skip_booking_queries_and_generation(self):
        from datetime import timedelta
        from unittest.mock import patch
        from sqlalchemy import event
        from app import admin_reports
        from app.models import Booking
        now = datetime(2026, 5, 5, 8, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            end = now - timedelta(days=2)
            db.add(Booking(guest_name='Existing', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
        admin_reports.enqueue_due_reports(self.sessions, now)
        booking_queries = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT') and 'FROM bookings' in statement:
                booking_queries.append(statement)
        event.listen(self.engine, 'before_cursor_execute', capture)
        try:
            with patch.object(admin_reports, '_student_groups', wraps=admin_reports._student_groups) as groups:
                admin_reports.enqueue_due_reports(self.sessions, now)
            self.assertEqual(booking_queries, [])
            groups.assert_not_called()
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture)

    def test_partial_recipients_only_insert_missing_reports(self):
        from datetime import timedelta
        from unittest.mock import patch
        from sqlalchemy import select
        from app import admin_reports, notifications
        from app.models import Booking, NotificationEvent
        now = datetime(2026, 5, 5, 8, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            end = now - timedelta(days=2)
            db.add(Booking(guest_name='Partial', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
        with patch.object(notifications, 'DEFAULT_RECIPIENTS', ['first@example.com']):
            admin_reports.enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            original = {e.logical_key: (e.id, e.body) for e in db.scalars(select(NotificationEvent))}
        with patch.object(notifications, 'DEFAULT_RECIPIENTS', ['first@example.com', 'second@example.com']), \
             patch.object(admin_reports, '_insert_report', wraps=admin_reports._insert_report) as insert:
            admin_reports.enqueue_due_reports(self.sessions, now)
        self.assertEqual(insert.call_count, 2)
        self.assertTrue(all(call.args[2] == 'second@example.com' for call in insert.call_args_list))
        with self.sessions() as db:
            events = list(db.scalars(select(NotificationEvent)))
            self.assertEqual(len(events), 4)
            self.assertEqual({e.logical_key: (e.id, e.body) for e in events if e.logical_key in original}, original)

    def test_reports_group_students_and_count_statuses(self):
        from datetime import timedelta
        from sqlalchemy import select
        from app.models import Booking, NotificationEvent
        from app.admin_reports import enqueue_due_reports
        with self.sessions.begin() as db:
            for day, state in [(1, 'paid'), (2, 'pending_payment'), (3, None)]:
                start = datetime(2025, 12, day, 12, tzinfo=timezone.utc)
                db.add(Booking(guest_name='Zeta', guest_email='z@example.com', starts_at=start, ends_at=start + timedelta(hours=1), status='confirmed', management_status=state))
        enqueue_due_reports(self.sessions, datetime(2026, 1, 1, 9, tzinfo=timezone.utc))
        with self.sessions() as db:
            body = db.scalar(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:monthly:%'))).body
            self.assertIn('Total: 3', body)
            self.assertIn('Pagadas: 1', body)
            self.assertIn('Pendientes de pago: 1', body)
            self.assertIn('Sin gestionar: 1', body)

    def test_report_retry_and_actual_mime_escape_preserve_snapshot(self):
        from datetime import timedelta
        from unittest.mock import patch
        from sqlalchemy import select
        from app import notifications
        from app.models import Booking, NotificationEvent
        from app.admin_reports import enqueue_due_reports
        now = datetime(2026, 5, 1, 8, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            end = now - timedelta(days=2)
            db.add(Booking(guest_name='<script>A&B</script>', note='<b>Química</b>', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
        enqueue_due_reports(self.sessions, now)
        with self.sessions.begin() as db:
            event = db.scalar(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:daily:%')))
            event_id, original = event.id, event.body
            # Only dispatch the daily report in this test.
            for other in db.scalars(select(NotificationEvent).where(NotificationEvent.id != event_id)):
                other.status = 'cancelled'
        messages = []
        class SMTP:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def send_message(self, message):
                messages.append(message)
                if len(messages) == 1: raise OSError('temporary failure')
        with patch.object(notifications.settings, 'smtp_mode', 'smtp'), patch.object(notifications.settings, 'smtp_host', 'test.invalid'), \
             patch.object(notifications.settings, 'smtp_ssl', False), patch.object(notifications.settings, 'smtp_starttls', False), \
             patch.object(notifications.settings, 'smtp_username', ''), patch.object(notifications.smtplib, 'SMTP', SMTP):
            self.assertEqual(notifications.deliver_pending(self.sessions), 0)
            with self.sessions.begin() as db:
                event = db.get(NotificationEvent, event_id)
                self.assertEqual((event.status, event.attempts, event.body), ('pending', 1, original))
                event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            enqueue_due_reports(self.sessions, now + timedelta(hours=1))
            self.assertEqual(notifications.deliver_pending(self.sessions), 1)
        with self.sessions() as db:
            self.assertEqual((db.get(NotificationEvent, event_id).status, db.get(NotificationEvent, event_id).body), ('sent', original))
        plain = messages[-1].get_body(preferencelist=('plain',)).get_content()
        html = messages[-1].get_body(preferencelist=('html',)).get_content()
        self.assertIn('<script>A&B</script>', plain)
        self.assertIn('&lt;script&gt;A&amp;B&lt;/script&gt;', html)
        self.assertIn('&lt;b&gt;Química&lt;/b&gt;', html)
        self.assertNotIn('<script>', html)

    def test_failed_generation_rolls_back_and_retries_transactionally(self):
        from datetime import timedelta
        from unittest.mock import patch
        from sqlalchemy import select
        from app.models import Booking, NotificationEvent
        from app import admin_reports
        now = datetime(2026, 5, 1, 8, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            end = now - timedelta(days=2)
            db.add(Booking(guest_name='Retry', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
        original = admin_reports._insert_report
        calls = []
        def fail_second(*args):
            calls.append(1)
            if len(calls) == 2: raise RuntimeError('failure')
            return original(*args)
        with patch.object(admin_reports, '_insert_report', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                admin_reports.enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(NotificationEvent))), [])
        admin_reports.enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(NotificationEvent)))), 2)

    def test_worker_schedules_reports_after_schema_migration(self):
        from unittest.mock import patch
        from app import notification_worker as worker
        calls = []
        with patch.object(worker, 'ensure_application_schema', side_effect=lambda *_: calls.append('create')), \
             patch.object(worker, 'ensure_notification_lease_column', side_effect=lambda: calls.append('lease')), \
             patch.object(worker, 'ensure_booking_management_column', side_effect=lambda: calls.append('management')), \
             patch.object(worker, 'enqueue_due_reports', side_effect=lambda *_: calls.append('reports')), \
             patch.object(worker, 'enqueue_due_reminders', return_value=0), \
             patch.object(worker, 'deliver_pending', return_value=0):
            worker.run_once()
        self.assertLess(calls.index('create'), calls.index('management'))
        self.assertLess(calls.index('management'), calls.index('reports'))

    def test_monthly_reports_previous_calendar_month_and_carryover(self):
        from app import models
        from app.admin_reports import enqueue_due_reports
        from sqlalchemy import select
        from datetime import timedelta
        now = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
        with self.sessions() as db:
            user = models.User(name='Alumno', email='a@example.com', password_hash='x')
            db.add(user); db.flush()
            for name, day, management, owner in [('Pagada', 10, 'paid', user.id), ('Invitada', 20, 'pending_payment', None), ('Sin gestionar', 25, None, None)]:
                start = datetime(2025, 12, day, 12, tzinfo=timezone.utc)
                db.add(models.Booking(user_id=owner, guest_name=name, guest_email='g@example.com', note=name, starts_at=start, ends_at=start+timedelta(hours=1), status='confirmed', management_status=management))
            start = datetime(2025, 11, 1, 12, tzinfo=timezone.utc)
            db.add(models.Booking(guest_name='Arrastre', starts_at=start, ends_at=start+timedelta(hours=1), status='confirmed'))
            db.commit()
        enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            event = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('report:monthly:%')))
            self.assertIsNotNone(event)
            for value in ('2025-12', 'Alumno', 'Pagada', 'Invitada', 'Sin gestionar', 'Arrastre', 'Pendiente de pago'):
                self.assertIn(value, event.body)
            self.assertNotIn('pending_payment', event.body)
            self.assertNotIn('paid', event.body)
            self.assertIn('Arrastre anterior', event.body)

    def test_monthly_recovery_uses_first_day_cutoff_and_carries_pending_payment(self):
        from datetime import timedelta
        from sqlalchemy import select
        from app.models import Booking, NotificationEvent
        from app.admin_reports import enqueue_due_reports
        with self.sessions.begin() as db:
            for name, start, end, status in [
                ('Deuda antigua', datetime(2025, 11, 1, 12, tzinfo=timezone.utc), datetime(2025, 11, 1, 13, tzinfo=timezone.utc), 'pending_payment'),
                ('Antes corte', datetime(2025, 12, 31, 8, tzinfo=timezone.utc), datetime(2025, 12, 31, 9, tzinfo=timezone.utc), None),
                ('Despues corte', datetime(2025, 12, 31, 10, tzinfo=timezone.utc), datetime(2026, 1, 2, 10, tzinfo=timezone.utc), None),
            ]:
                db.add(Booking(guest_name=name, guest_email='same@example.com', starts_at=start, ends_at=end, status='confirmed', management_status=status))
        enqueue_due_reports(self.sessions, datetime(2026, 1, 15, 9, tzinfo=timezone.utc))
        with self.sessions() as db:
            event = db.scalar(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:monthly:2025-12:%')))
            self.assertIsNotNone(event)
            self.assertIn('Deuda antigua', event.body)
            self.assertIn('Pendiente de pago', event.body)
            self.assertIn('Antes corte', event.body)
            self.assertNotIn('Despues corte', event.body)

    def test_worker_report_failure_does_not_block_reminders_or_delivery(self):
        from unittest.mock import patch
        from app import notification_worker as worker
        with patch.object(worker, 'ensure_application_schema'), patch.object(worker, 'ensure_notification_lease_column'), \
             patch.object(worker, 'ensure_booking_management_column'), \
             patch.object(worker, 'enqueue_due_reports', side_effect=RuntimeError('sensitive@example.com')), \
             patch.object(worker, 'enqueue_due_reminders', return_value=2), \
             patch.object(worker, 'deliver_pending', return_value=3), self.assertLogs(level='ERROR') as logs:
            self.assertEqual(worker.run_once(), (2, 3))
            self.assertNotIn('sensitive@example.com', ' '.join(logs.output))

    def test_empty_daily_not_queued_and_later_recovers_today(self):
        from app.admin_reports import enqueue_due_reports
        from app.models import NotificationEvent, Booking
        from sqlalchemy import select
        from datetime import timedelta
        now = datetime(2026, 5, 5, 8, tzinfo=timezone.utc)
        enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:daily:%')))), [])
            end = now - timedelta(days=2)
            db.add(Booking(guest_name='Late', starts_at=end-timedelta(hours=1), ends_at=end, status='confirmed'))
            db.commit()
        enqueue_due_reports(self.sessions, now + timedelta(hours=3))
        with self.sessions() as db:
            self.assertIn('Late', db.scalar(select(NotificationEvent).where(NotificationEvent.logical_key.like('report:daily:%'))).body)

    def test_daily_strict_cutoff_snapshot_dedup_and_next_day(self):
        from app import models
        from app.admin_reports import enqueue_due_reports
        from sqlalchemy import select
        from datetime import timedelta
        now = datetime(2026, 5, 5, 8, tzinfo=timezone.utc)
        with self.sessions() as db:
            for name, end in [("Eligible", now - timedelta(days=1, seconds=1)), ("Boundary", now - timedelta(days=1))]:
                db.add(models.Booking(guest_name=name, guest_email="guest@example.com", starts_at=end-timedelta(hours=1), ends_at=end, status="confirmed"))
            db.commit()
        enqueue_due_reports(self.sessions, now)
        enqueue_due_reports(self.sessions, now)
        with self.sessions() as db:
            events = list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('report:daily:%'))))
            self.assertEqual(len(events), 1)
            self.assertIn("Eligible", events[0].body)
            self.assertNotIn("Boundary", events[0].body)
        enqueue_due_reports(self.sessions, now + timedelta(days=1))
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('report:daily:%'))))), 2)
