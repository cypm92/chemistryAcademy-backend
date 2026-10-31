"""Delivery seam for future email notifications.

SMTP/provider integration deliberately lives here so booking logic stays unchanged
when real email delivery is enabled later.
"""
import logging
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from sqlalchemy import or_, select, update
from . import models
from .config import settings

from .models import Booking, ContactRequest, User

logger = logging.getLogger(__name__)
MADRID = ZoneInfo("Europe/Madrid")
DEFAULT_RECIPIENTS = ["clases@beciencia.es"]
PROCESSING_LEASE = timedelta(minutes=5)


def notification_settings(db):
    interval = db.get(models.AppSetting, "reminder_interval_minutes")
    return {"reminder_interval_minutes": int(interval.value) if interval else 1440}


def local_time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MADRID).strftime("%d/%m/%Y %H:%M")


def enqueue(db, logical_key, recipient, subject, body):
    if recipient and not db.scalar(select(models.NotificationEvent.id).where(models.NotificationEvent.logical_key == logical_key)):
        db.add(models.NotificationEvent(logical_key=logical_key, recipient=recipient, subject=subject, body=body))


def enqueue_booking_request(db, booking):
    name = booking.user.name if booking.user else (booking.guest_name or "")
    email = booking.user.email if booking.user else booking.guest_email
    body = f"Nueva solicitud de {name}: {local_time(booking.starts_at)}–{local_time(booking.ends_at)} ({booking.note})"
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, f"booking:{booking.id}:requested:admin:{address}", address, "Nueva solicitud de clase", body)
    enqueue(db, f"booking:{booking.id}:requested:student", email, "Solicitud de clase recibida", body)


def enqueue_booking_decision(db, booking):
    email = booking.user.email if booking.user else booking.guest_email
    body = f"Solicitud {booking.status}: {local_time(booking.starts_at)}–{local_time(booking.ends_at)} ({booking.note})"
    enqueue(db, f"booking:{booking.id}:{booking.status}:student", email, "Actualización de tu solicitud", body)


def enqueue_booking_change(db, booking, action, previous_start=None):
    for event in db.scalars(select(models.NotificationEvent).where(
        models.NotificationEvent.logical_key.like(f"booking:{booking.id}:reminder:%"),
        models.NotificationEvent.status == "pending")):
        event.status = "cancelled"
    email = booking.user.email if booking.user else booking.guest_email
    body = f"{action}: {local_time(booking.starts_at)}–{local_time(booking.ends_at)} ({booking.note})"
    if previous_start:
        body += f"; horario anterior: {local_time(previous_start)}"
    key = f"booking:{booking.id}:{action}:{booking.starts_at.isoformat()}"
    enqueue(db, key + ":student", email, action, body)
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, key + ":admin:" + address, address, action, body)


def _send(event):
    if settings.smtp_mode.lower() != "smtp" or not settings.smtp_host:
        logger.info("Email dry-run (delivery disabled)")
        return False
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.smtp_sender, event.recipient, event.subject
    message.set_content(event.body)
    transport = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    with transport(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls and not settings.smtp_ssl:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return True


def deliver_pending(session_factory, batch_size=50):
    sent = 0
    with session_factory() as db:
        now = datetime.now(timezone.utc)
        event_ids = list(db.scalars(select(models.NotificationEvent.id).where(or_(
            (models.NotificationEvent.status == "pending") & (models.NotificationEvent.available_at <= now),
            (models.NotificationEvent.status == "processing") & (models.NotificationEvent.lease_expires_at <= now),
        )).order_by(models.NotificationEvent.id).limit(batch_size)))
        db.commit()
        for event_id in event_ids:
            claimed_at = datetime.now(timezone.utc)
            result = db.execute(update(models.NotificationEvent).where(
                models.NotificationEvent.id == event_id,
                or_((models.NotificationEvent.status == "pending") & (models.NotificationEvent.available_at <= claimed_at),
                    (models.NotificationEvent.status == "processing") & (models.NotificationEvent.lease_expires_at <= claimed_at)),
            ).values(status="processing", lease_expires_at=claimed_at + PROCESSING_LEASE))
            db.commit()
            if not result.rowcount:
                continue
            event = db.get(models.NotificationEvent, event_id)
            try:
                was_sent = _send(event)
                event.status = "sent" if was_sent else "simulated"
                event.sent_at = datetime.now(timezone.utc) if was_sent else None
                event.last_error, event.lease_expires_at = None, None
                if was_sent:
                    sent += 1
            except Exception as exc:
                event.attempts += 1
                event.status, event.last_error, event.lease_expires_at = "pending", str(exc)[:2000], None
                event.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * 2 ** min(event.attempts - 1, 7)))
                logger.exception("Email delivery failed for event %s", event.id)
            db.commit()
    return sent


def enqueue_due_reminders(session_factory):
    with session_factory() as db:
        interval = notification_settings(db)["reminder_interval_minutes"]
        now = datetime.now(timezone.utc)
        bookings = list(db.scalars(select(models.Booking).where(models.Booking.status == "confirmed",
            models.Booking.starts_at > now, models.Booking.starts_at <= now + timedelta(minutes=interval))))
        for booking in bookings:
            email = booking.user.email if booking.user else booking.guest_email
            body = f"Recordatorio de clase: {local_time(booking.starts_at)}–{local_time(booking.ends_at)} ({booking.note})"
            enqueue(db, f"booking:{booking.id}:reminder:{booking.starts_at.isoformat()}", email, "Recordatorio de tu clase", body)
        db.commit()
        return len(bookings)


def enqueue_contact_request(db, request):
    body = f"Nueva solicitud de contacto: {request.name}; {request.contact}; {request.need}; {request.message}"
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, f"contact:{request.id}:admin:{address}", address, "Nueva solicitud de contacto", body)


def notify_admin_of_booking_request(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: nueva solicitud de %s para %s", student.email, booking.starts_at.isoformat())


def notify_student_of_booking_decision(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: solicitud %s para %s", booking.status, student.email)


def notify_admin_of_contact_request(request: ContactRequest) -> None:
    logger.info("Email pendiente: nueva solicitud de contacto de %s (%s)", request.name, request.contact)
