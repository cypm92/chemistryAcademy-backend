"""Delivery seam for future email notifications.

SMTP/provider integration deliberately lives here so booking logic stays unchanged
when real email delivery is enabled later.
"""
import logging

from .models import Booking, ContactRequest, User

logger = logging.getLogger(__name__)


def notify_admin_of_booking_request(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: nueva solicitud de %s para %s", student.email, booking.starts_at.isoformat())


def notify_student_of_booking_decision(student: User, booking: Booking) -> None:
    logger.info("Email pendiente: solicitud %s para %s", booking.status, student.email)


def notify_admin_of_contact_request(request: ContactRequest) -> None:
    logger.info("Email pendiente: nueva solicitud de contacto de %s (%s)", request.name, request.contact)
