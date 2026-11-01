"""Generate synthetic notifications for the isolated Mailpit compose project."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from . import models, notifications


def examples():
    engine = create_engine('sqlite://')
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    saved_recipients = notifications.DEFAULT_RECIPIENTS
    notifications.DEFAULT_RECIPIENTS = ['administracion@example.invalid']
    result = []
    try:
        with factory() as db:
            start = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
            booking = models.Booking(guest_name='Lucía Ejemplo', guest_email='alumna@example.invalid',
                                     starts_at=start, ends_at=start + timedelta(hours=1),
                                     status='requested', note='Química de ejemplo')
            db.add(booking)
            db.flush()

            def capture():
                db.flush()
                for event in db.scalars(select(models.NotificationEvent)).all():
                    result.append(SimpleNamespace(recipient=event.recipient, subject=event.subject, body=event.body))
                    db.delete(event)
                db.flush()

            notifications.enqueue_booking_request(db, booking)
            capture()
            for status in ('confirmed', 'declined'):
                booking.status = status
                notifications.enqueue_booking_decision(db, booking)
                capture()
            for action in ('Clase reprogramada', 'Clase cancelada'):
                previous = start - timedelta(hours=1) if action == 'Clase reprogramada' else None
                notifications.enqueue_booking_change(db, booking, action, previous_start=previous)
                capture()
            notifications.enqueue_booking_change(db, booking, 'Clase cancelada', was_pending=True)
            capture()
            contact = models.ContactRequest(id=1, name='Lucía Ejemplo', contact='alumna@example.invalid',
                                            need='Química', message='Mensaje de ejemplo')
            notifications.enqueue_contact_request(db, contact)
            capture()
            booking.status = 'confirmed'
            db.commit()
        # Freeze only the preview scheduler so the example is due, independent of the real clock.
        with patch.object(notifications, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = start - timedelta(hours=1)
            notifications.enqueue_due_reminders(factory)
        with factory() as db:
            for event in db.scalars(select(models.NotificationEvent)).all():
                result.append(SimpleNamespace(recipient=event.recipient, subject=event.subject, body=event.body))
    finally:
        notifications.DEFAULT_RECIPIENTS = saved_recipients
        engine.dispose()
    return result


def send_examples():
    settings = notifications.settings
    if (settings.smtp_mode != 'smtp' or settings.smtp_host != 'mailpit' or settings.smtp_port != 1025
            or settings.smtp_ssl or settings.smtp_starttls or settings.smtp_username
            or settings.smtp_password or settings.smtp_sender != 'preview@example.invalid'):
        raise RuntimeError('Configuración SMTP de previsualización insegura')
    events = examples()
    if any(not event.recipient.endswith('@example.invalid') for event in events):
        raise RuntimeError('Destinatario no reservado para ejemplos')
    for event in events:
        if not notifications._send(event):
            raise RuntimeError('No se capturó el correo de ejemplo')
    print(f'{len(events)} correos de ejemplo enviados a Mailpit')


if __name__ == '__main__':
    send_examples()
