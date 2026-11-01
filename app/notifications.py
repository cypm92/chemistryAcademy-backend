"""Delivery seam for future email notifications.

SMTP/provider integration deliberately lives here so booking logic stays unchanged
when real email delivery is enabled later.
"""
import logging
import json
import smtplib
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
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
    local = value.astimezone(MADRID)
    days = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
    months = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
              'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')
    offset = ''
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        raw = local.strftime('%z')
        offset = f' UTC{raw[:3]}:{raw[3:]}'
    return (f'{days[local.weekday()]} {local.day} de {months[local.month - 1]} de {local.year}, '
            f'a las {local:%H:%M}{offset} (hora de Madrid)')


def local_range(start, end):
    def in_madrid(value):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(MADRID)

    def date_label(value):
        days = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
        months = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                  'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')
        return f'{days[value.weekday()]} {value.day} de {months[value.month - 1]} de {value.year}'

    def offset(value):
        raw = value.strftime('%z')
        return f'UTC{raw[:3]}:{raw[3:]}'

    def ambiguous(value):
        return value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset()

    first, last = in_madrid(start), in_madrid(end)
    if first.utcoffset() != last.utcoffset() or ambiguous(first) or ambiguous(last):
        return (f'{date_label(first)} a las {first:%H:%M} ({offset(first)}) hasta '
                f'{date_label(last)} a las {last:%H:%M} ({offset(last)}) (hora de Madrid)')
    if first.date() != last.date():
        return f'{date_label(first)} a las {first:%H:%M} hasta {date_label(last)} a las {last:%H:%M} (hora de Madrid)'
    return f'{date_label(first)}, de {first:%H:%M} a {last:%H:%M} (hora de Madrid)'


def booking_details(booking):
    return f"Fecha y hora: {local_range(booking.starts_at, booking.ends_at)}\nAsignatura o nota: {booking.note or 'No indicada'}"


def student_name(booking):
    return (booking.user.name if booking.user else booking.guest_name) or "estudiante"


def enqueue(db, logical_key, recipient, subject, body):
    if recipient and not db.scalar(select(models.NotificationEvent.id).where(models.NotificationEvent.logical_key == logical_key)):
        db.add(models.NotificationEvent(logical_key=logical_key, recipient=recipient, subject=subject, body=body))


def enqueue_booking_request(db, booking, revision=None):
    name = booking.user.name if booking.user else (booking.guest_name or "")
    email = booking.user.email if booking.user else booking.guest_email
    body = (f"Hola, equipo:\n\nHay una nueva solicitud de clase pendiente de revisión.\n\n"
            f"Alumno: {name or 'No indicado'}\nCorreo: {email or 'No indicado'}\n{booking_details(booking)}\n\n"
            "Gracias,\nEquipo BeCiencia")
    suffix = f":{revision}" if revision else ""
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, f"booking:{booking.id}:requested:admin:{address}{suffix}", address, "Nueva solicitud de clase", body)
    student_body = (f"Hola, {name or 'estudiante'}:\n\nGracias por solicitar una clase. "
                    f"Tu solicitud está pendiente de confirmación. Te avisaremos cuando la revisemos.\n\n"
                    f"{booking_details(booking)}\n\nUn saludo,\nEquipo BeCiencia")
    enqueue(db, f"booking:{booking.id}:requested:student{suffix}", email, "Solicitud de clase recibida", student_body)


def _invalidate_booking_events(db, booking_id, kinds):
    prefix = f"booking:{booking_id}:"
    db.execute(update(models.NotificationEvent).where(
        or_(*(models.NotificationEvent.logical_key.like(prefix + kind + ":%") for kind in kinds)),
        models.NotificationEvent.status.in_(("pending", "processing")),
    ).values(status="cancelled", lease_expires_at=None))


def refresh_booking_request(db, booking):
    _invalidate_booking_events(db, booking.id, ("requested",))
    enqueue_booking_request(db, booking, uuid4().hex)


def enqueue_booking_decision(db, booking):
    _invalidate_booking_events(db, booking.id, ("requested",))
    email = booking.user.email if booking.user else booking.guest_email
    subject = "Clase confirmada" if booking.status == "confirmed" else "Solicitud de clase rechazada"
    message = ("Tu clase está confirmada. ¡Nos vemos pronto!" if booking.status == "confirmed"
               else "Sentimos comunicarte que no hemos podido aceptar tu solicitud de clase.")
    body = (f"Hola, {student_name(booking)}:\n\n{message}\n\n{booking_details(booking)}"
            "\n\nSi tienes alguna pregunta, contacta con nosotros.\n\nUn saludo,\nEquipo BeCiencia")
    enqueue(db, f"booking:{booking.id}:{booking.status}:student", email, subject, body)


def enqueue_booking_change(db, booking, action, previous_start=None, was_pending=False):
    _invalidate_booking_events(db, booking.id,
        ("requested", "confirmed", "declined", "reminder", "Clase reprogramada"))
    email = booking.user.email if booking.user else booking.guest_email
    pending_cancellation = action == "Clase cancelada" and was_pending
    subject = "Solicitud de clase cancelada" if pending_cancellation else action
    message = ("Tu clase se ha reprogramado. Este es el nuevo horario:"
               if action == "Clase reprogramada" else
               "Tu solicitud pendiente de confirmación ha sido cancelada. Estos son sus datos:"
               if pending_cancellation else "Tu clase ha sido cancelada. Estos son los datos de la reserva:")
    details = booking_details(booking)
    if previous_start:
        details += f"\nHorario anterior: {local_time(previous_start)}"
    body = (f"Hola, {student_name(booking)}:\n\n{message}\n\n{details}"
            "\n\nSi tienes alguna pregunta, contacta con nosotros.\n\nUn saludo,\nEquipo BeCiencia")
    admin_body = (f"Hola, equipo:\n\n{subject}.\nAlumno: {student_name(booking)}\n"
                  f"Correo: {email or 'No indicado'}\n{details}\n\nEquipo BeCiencia")
    start = booking.starts_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    key = f"booking:{booking.id}:{action}:{start.astimezone(timezone.utc).isoformat()}~{uuid4().hex}"
    enqueue(db, key + ":student", email, subject, body)
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, key + ":admin:" + address, address, subject, admin_body)


def _send(event):
    if settings.smtp_mode.lower() != "smtp" or not settings.smtp_host:
        logger.info("Email dry-run (delivery disabled)")
        return False
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.smtp_sender, event.recipient, event.subject
    message.set_content(event.body)
    # Render the persisted text at delivery time, including legacy outbox rows.
    # Dynamic values are text only: never interpolate them into attributes or URLs.
    title = escape(event.subject)
    paragraphs = []
    for block in event.body.split("\n\n"):
        content = escape(block).replace("\n", "<br>\n")
        if block.startswith("Fecha y hora:"):
            paragraphs.append(f'<p class="booking-details" style="margin:0 0 20px;padding:16px;background-color:#f0f6f3;border-left:3px solid #397b69;border-radius:6px;">{content}</p>')
        else:
            paragraphs.append(f'<p style="margin:0 0 18px;">{content}</p>')
    body = "\n".join(paragraphs)
    message.add_alternative(f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f3f6f8;font-family:Arial,Helvetica,sans-serif;color:#233242;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:#f3f6f8;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background-color:#ffffff;">
<tr><td style="background-color:#153d49;padding:24px 28px;color:#ffffff;font-size:22px;font-weight:bold;">BeCiencia</td></tr>
<tr><td style="padding:28px;line-height:1.6;font-size:16px;"><h1 style="font-size:22px;line-height:1.3;margin:0 0 20px;color:#153d49;">{title}</h1>{body}</td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e3e9eb;color:#64747a;font-size:13px;">BeCiencia · Notificación de la academia</td></tr>
</table></td></tr></table></body></html>''', subtype="html")
    transport = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    with transport(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls and not settings.smtp_ssl:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return True


def _booking_event_current(db, event):
    parts = event.logical_key.split(":", 3)
    if len(parts) < 4 or parts[0] != "booking" or not parts[1].isdigit():
        return True
    kind = parts[2]
    expected = {"requested": "requested", "confirmed": "confirmed", "declined": "declined",
                "reminder": "confirmed", "Clase reprogramada": "confirmed", "Clase cancelada": "cancelled"}
    if kind not in expected:
        return True
    booking = db.get(models.Booking, int(parts[1]), populate_existing=True)
    if not booking:
        return kind == "Clase cancelada"
    if booking.status != expected[kind]:
        return False
    if kind in {"reminder", "Clase reprogramada", "Clase cancelada"}:
        stamp = parts[3]
        if kind != "reminder":
            stamp = stamp.rsplit(":student", 1)[0].rsplit(":admin:", 1)[0]
        try:
            value = datetime.fromisoformat(stamp.split("~", 1)[0])
            current = booking.starts_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return value == current and (kind != "reminder" or current > datetime.now(timezone.utc))
        except ValueError:
            return False
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
            event = db.scalar(select(models.NotificationEvent).where(
                models.NotificationEvent.id == event_id,
                models.NotificationEvent.status == "processing",
                models.NotificationEvent.lease_expires_at == claimed_at + PROCESSING_LEASE,
            ).execution_options(populate_existing=True))
            if not event or event.status != "processing" or not _booking_event_current(db, event):
                if event and event.status == "processing":
                    db.execute(update(models.NotificationEvent).where(
                        models.NotificationEvent.id == event_id,
                        models.NotificationEvent.status == "processing",
                        models.NotificationEvent.lease_expires_at == claimed_at + PROCESSING_LEASE,
                    ).values(status="cancelled", lease_expires_at=None))
                    db.commit()
                continue
            lease = claimed_at + PROCESSING_LEASE
            try:
                was_sent = _send(event)
                values = dict(status="sent" if was_sent else "simulated",
                              sent_at=datetime.now(timezone.utc) if was_sent else None,
                              last_error=None, lease_expires_at=None)
            except Exception as exc:
                attempts = event.attempts + 1
                values = dict(attempts=attempts, status="pending", last_error=str(exc)[:2000],
                              lease_expires_at=None, available_at=datetime.now(timezone.utc) +
                              timedelta(seconds=min(3600, 30 * 2 ** min(attempts - 1, 7))))
                was_sent = False
                logger.exception("Email delivery failed for event %s", event.id)
            result = db.execute(update(models.NotificationEvent).where(
                models.NotificationEvent.id == event_id,
                models.NotificationEvent.status == "processing",
                models.NotificationEvent.lease_expires_at == lease,
            ).values(**values))
            if was_sent and not result.rowcount:
                db.execute(update(models.NotificationEvent).where(
                    models.NotificationEvent.id == event_id,
                    models.NotificationEvent.status == "cancelled",
                    models.NotificationEvent.sent_at.is_(None),
                ).values(sent_at=values["sent_at"], last_error="SMTP accepted after cancellation"))
            db.commit()
            if was_sent:
                sent += 1
    return sent


def enqueue_due_reminders(session_factory):
    with session_factory() as db:
        interval = notification_settings(db)["reminder_interval_minutes"]
        now = datetime.now(timezone.utc)
        # Keep booking snapshot and notification generation in one transaction.
        bookings = list(db.scalars(select(models.Booking).where(models.Booking.status == "confirmed",
            models.Booking.starts_at > now, models.Booking.starts_at <= now + timedelta(minutes=interval))
            .order_by(models.Booking.id).with_for_update()))
        for booking in bookings:
            email = booking.user.email if booking.user else booking.guest_email
            body = (f"Hola, {student_name(booking)}:\n\nTe recordamos que tienes una clase confirmada.\n\n"
                    f"{booking_details(booking)}\n\n¡Hasta pronto!\nEquipo BeCiencia")
            latest_change = db.scalar(select(models.NotificationEvent.logical_key).where(
                models.NotificationEvent.logical_key.like(f"booking:{booking.id}:Clase reprogramada:%")
            ).order_by(models.NotificationEvent.id.desc()).limit(1))
            revision = latest_change.split("~", 1)[1].split(":", 1)[0] if latest_change and "~" in latest_change else None
            suffix = f"~{revision}" if revision else ""
            enqueue(db, f"booking:{booking.id}:reminder:{booking.starts_at.isoformat()}{suffix}", email, "Recordatorio de tu clase", body)
        db.commit()
        return len(bookings)


def enqueue_contact_request(db, request):
    body = (f"Hola, equipo:\n\nHay una nueva solicitud de contacto pendiente de atención.\n\n"
            f"Nombre: {request.name}\nDatos de contacto: {request.contact}\n"
            f"Necesidad: {request.need}\nMensaje: {request.message or 'No indicado'}\n\nEquipo BeCiencia")
    for address in DEFAULT_RECIPIENTS:
        enqueue(db, f"contact:{request.id}:admin:{address}", address, "Nueva solicitud de contacto", body)


def notify_admin_of_booking_request(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: nueva solicitud de %s para %s", student.email, booking.starts_at.isoformat())


def notify_student_of_booking_decision(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: solicitud %s para %s", booking.status, student.email)


def notify_admin_of_contact_request(request: ContactRequest) -> None:
    logger.info("Email pendiente: nueva solicitud de contacto de %s (%s)", request.name, request.contact)
