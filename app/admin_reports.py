"""Informes diarios y del mes anterior, con corte en hora de Madrid."""
from datetime import timedelta
from .notifications import MADRID


def _student_groups(bookings):
    from .notifications import booking_details, student_name

    groups = {}
    for booking in bookings:
        email = (booking.user.email if booking.user else booking.guest_email) or 'No indicado'
        identity = ('user', booking.user_id) if booking.user_id else ('guest', email, booking.guest_name)
        group = groups.setdefault(identity, {'name': student_name(booking), 'email': email, 'bookings': []})
        group['bookings'].append(booking)
    result = []
    labels = {'paid': 'Pagada', 'pending_payment': 'Pendiente de pago', None: 'Sin gestionar'}
    for group in sorted(groups.values(), key=lambda g: (g['name'].casefold(), g['email'].casefold())):
        items = group['bookings']
        counts = [sum(b.management_status == value for b in items) for value in ('paid', 'pending_payment', None)]
        header = (f"Alumno: {group['name']}\nCorreo: {group['email']}\n"
                  f"Total: {len(items)} | Pagadas: {counts[0]} | Pendientes de pago: {counts[1]} | Sin gestionar: {counts[2]}")
        details = [f"Estado de gestión: {labels[b.management_status]}\n{booking_details(b)}" for b in items]
        result.append('\n\n'.join([header, *details]))
    return result


def due_periods(now):
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    local = now.astimezone(MADRID)
    previous = local.date().replace(day=1) - timedelta(days=1)
    daily = local.date().isoformat() if local.hour >= 10 else None
    monthly = previous.strftime('%Y-%m') if local.day > 1 or local.hour >= 10 else None
    return daily, monthly


def enqueue_due_reports(session_factory, now=None):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from .models import Booking, NotificationEvent
    from .notifications import DEFAULT_RECIPIENTS

    now = now or datetime.now(timezone.utc)
    daily, monthly = due_periods(now)
    if not daily and not monthly:
        return
    cutoff = now.astimezone(MADRID).replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    with session_factory() as db:
        keys = {
            kind: {address: f'report:{kind}:{period}:{address}' for address in DEFAULT_RECIPIENTS}
            for kind, period in (('daily', daily), ('monthly', monthly)) if period
        }
        all_keys = [key for recipients in keys.values() for key in recipients.values()]
        existing = set(db.scalars(select(NotificationEvent.logical_key).where(
            NotificationEvent.logical_key.in_(all_keys)
        ))) if all_keys else set()
        missing = {kind: [address for address, key in recipients.items() if key not in existing]
                   for kind, recipients in keys.items()}
        if missing.get('daily'):
            bookings = list(db.scalars(select(Booking).where(
                Booking.status == "confirmed", Booking.management_status.is_(None),
                Booking.ends_at < cutoff - timedelta(hours=24),
            ).order_by(Booking.starts_at, Booking.id)))
            if bookings:
                body = "Clases confirmadas sin gestionar (más de 24 horas):\n\n" + "\n\n".join(_student_groups(bookings))
                for address in missing['daily']:
                    _insert_report(db, keys['daily'][address], address, "Clases pendientes de gestión", body)
        if missing.get('monthly'):
            from sqlalchemy import or_, and_
            monthly_cutoff = cutoff.astimezone(MADRID).replace(day=1).astimezone(timezone.utc)
            local_month_end = monthly_cutoff.astimezone(MADRID).replace(hour=0)
            local_month_start = (local_month_end - timedelta(days=1)).replace(day=1)
            start = local_month_start.astimezone(timezone.utc)
            end = local_month_end.astimezone(timezone.utc)
            bookings = list(db.scalars(select(Booking).where(
                Booking.status == 'confirmed',
                or_(and_(Booking.starts_at >= start, Booking.starts_at < end,
                         or_(Booking.management_status.in_(['paid', 'pending_payment']),
                             and_(Booking.management_status.is_(None), Booking.ends_at < monthly_cutoff))),
                    and_(Booking.starts_at < start, or_(Booking.management_status == 'pending_payment',
                         and_(Booking.management_status.is_(None), Booking.ends_at < monthly_cutoff))))
            ).order_by(Booking.starts_at, Booking.id)))
            sections = {'Mes anterior': [], 'Arrastre anterior': []}
            for b in bookings:
                value = b.starts_at if b.starts_at.tzinfo else b.starts_at.replace(tzinfo=timezone.utc)
                section = 'Mes anterior' if value >= start else 'Arrastre anterior'
                sections[section].append(b)
            chunks = [f'Informe mensual {monthly}']
            for section, groups in sections.items():
                chunks.append(section)
                chunks.extend(_student_groups(groups))
                if not groups:
                    chunks.append('Sin clases')
            body = '\n\n'.join(chunks)
            for address in missing['monthly']:
                _insert_report(db, keys['monthly'][address], address, f'Informe mensual {monthly}', body)
        db.commit()


def _insert_report(db, key, recipient, subject, body):
    from .models import NotificationEvent
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Unsupported database dialect for report deduplication")
    db.execute(insert(NotificationEvent).values(logical_key=key, recipient=recipient, subject=subject, body=body)
               .on_conflict_do_nothing(index_elements=[NotificationEvent.logical_key]))
