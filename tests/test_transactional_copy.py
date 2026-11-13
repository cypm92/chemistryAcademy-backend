import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from ephemeral_postgres import ephemeral_postgres

from unittest.mock import patch
from app import models
from app.database import Base
from app.notifications import (enqueue_booking_request, enqueue_booking_decision, enqueue_booking_change,
                               enqueue_contact_request, enqueue_due_reminders, local_time)


class TransactionalCopyTest(unittest.TestCase):
    def postgres_engine(self):
        context = ephemeral_postgres()
        database = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return database.engine

    def test_html_uses_separate_paragraphs_and_highlights_class_details(self):
        from app.notifications import _send
        body = 'Hola, Ana:\n\nTu clase está lista.\n\nFecha y hora: lunes 2 de noviembre\nAsignatura o nota: Química\n\nUn saludo'
        event = models.NotificationEvent(logical_key='x', recipient='ana@example.invalid', subject='Tu clase', body=body)
        with patch('app.notifications.settings.smtp_mode', 'smtp'), patch('app.notifications.settings.smtp_host', 'mailpit'), patch('app.notifications.settings.smtp_ssl', True), patch('app.notifications.smtplib.SMTP_SSL') as transport:
            self.assertTrue(_send(event))
            message = transport.return_value.__enter__.return_value.send_message.call_args.args[0]
        html = message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('class="booking-details"', html)
        self.assertGreaterEqual(html.count('<p '), 2)
        self.assertEqual(message.get_body(preferencelist=('plain',)).get_content().rstrip('\n'), body)
    def test_request_gives_student_clear_pending_status_and_local_range(self):
        engine = self.postgres_engine()
        self.assertEqual(engine.dialect.name, 'postgresql')
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            start = datetime(2030, 1, 5, 9, tzinfo=timezone.utc)
            booking = models.Booking(guest_name='Ana', guest_email='ana@example.com', starts_at=start,
                                     ends_at=start + timedelta(hours=1), status='requested', note='Química')
            db.add(booking)
            db.flush()
            enqueue_booking_request(db, booking)
            db.flush()
            event = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.recipient == 'ana@example.com'))
            self.assertIn('Hola, Ana', event.body)
            self.assertIn('pendiente de confirmación', event.body)
            self.assertIn('sábado 5 de enero de 2030, de 10:00 a 11:00 (hora de Madrid)', event.body)
            self.assertIn('Química', event.body)
        engine.dispose()

    def test_scenario_messages_are_specific_and_admin_can_identify_student(self):
        engine = self.postgres_engine()
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            start = datetime(2030, 1, 5, 9, tzinfo=timezone.utc)
            booking = models.Booking(guest_name='Ana', guest_email='ana@example.com', starts_at=start,
                                     ends_at=start + timedelta(hours=1), status='requested', note='Química')
            db.add(booking); db.flush()
            enqueue_booking_request(db, booking)
            db.flush()
            admin = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('%:requested:admin:%')))
            self.assertIn('ana@example.com', admin.body)
            for status, phrase in [('confirmed', 'confirmada'), ('declined', 'no hemos podido')]:
                booking.status = status
                enqueue_booking_decision(db, booking); db.flush()
                event = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key == f'booking:{booking.id}:{status}:student'))
                self.assertIn(phrase, event.body)
                self.assertIn('Hola, Ana', event.body)
            booking.status = 'confirmed'
            enqueue_booking_change(db, booking, 'Clase reprogramada', start - timedelta(days=1))
            booking.status = 'cancelled'
            enqueue_booking_change(db, booking, 'Clase cancelada')
            db.flush()
            events = list(db.scalars(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('%:Clase %:student'))))
            self.assertEqual(len(events), 2)
            for event in events:
                self.assertIn('Hola, Ana', event.body)
            self.assertIn('Horario anterior', events[0].body)
            self.assertIn('cancelada', events[1].body)
        engine.dispose()

    def test_reminder_and_contact_copy_include_actionable_details(self):
        engine = self.postgres_engine()
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            start = datetime.now(timezone.utc) + timedelta(hours=1)
            db.add(models.Booking(guest_name='Ana', guest_email='ana@example.com', starts_at=start,
                                  ends_at=start + timedelta(hours=1), status='confirmed', note='Química'))
            contact = models.ContactRequest(name='Eva', contact='eva@example.com', need='Clases', message='Hola')
            db.add(contact); db.flush()
            enqueue_contact_request(db, contact); db.commit()
        enqueue_due_reminders(sessions)
        with sessions() as db:
            reminder = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('%:reminder:%')))
            self.assertIn('Hola, Ana', reminder.body)
            self.assertIn('recordamos', reminder.body)
            contact_event = db.scalar(select(models.NotificationEvent).where(models.NotificationEvent.logical_key.like('contact:%')))
            self.assertIn('Datos de contacto: eva@example.com', contact_event.body)
            self.assertIn('Mensaje: Hola', contact_event.body)
        engine.dispose()

    def test_madrid_range_handles_midnight_and_clock_change(self):
        from app.notifications import local_range
        utc = timezone.utc
        self.assertIn('30 de marzo de 2030', local_range(datetime(2030, 3, 30, 22, 30, tzinfo=utc), datetime(2030, 3, 30, 23, 30, tzinfo=utc)))
        self.assertIn('31 de marzo de 2030', local_range(datetime(2030, 3, 30, 22, 30, tzinfo=utc), datetime(2030, 3, 30, 23, 30, tzinfo=utc)))
        result = local_range(datetime(2030, 10, 27, 0, 30, tzinfo=utc), datetime(2030, 10, 27, 1, 30, tzinfo=utc))
        self.assertIn('UTC+02:00', result)
        self.assertIn('UTC+01:00', result)
