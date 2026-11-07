"""Run the durable notification queue and schedule reminders."""
import logging
import os
import time

import httpx

from .microsoft_oauth_routes import get_config
from .microsoft_refresh import get_access_token_with_identity
from .teams_sync import process_pending

from . import microsoft_auth_models, teams_models
from .database import SessionLocal, engine, ensure_notification_lease_column, ensure_booking_management_column, ensure_application_schema
from .admin_reports import enqueue_due_reports
from .notifications import deliver_pending, enqueue_due_reminders

logging.basicConfig(level=logging.INFO)


def run_once():
    ensure_application_schema(engine)
    ensure_notification_lease_column()
    ensure_booking_management_column()
    try:
        enqueue_due_reports(SessionLocal)
    except Exception as exc:
        # Exception messages and tracebacks may contain personal data or SQL parameters.
        logging.error("No se pudieron generar los informes (%s); se reintentará", type(exc).__name__)
    reminders = enqueue_due_reminders(SessionLocal)
    delivered = deliver_pending(SessionLocal)
    if os.environ.get('MICROSOFT_SYNC_ENABLED', '').strip().lower() == 'true':
        try:
            config = get_config()
            if config is not None:
                with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                    token_provider = lambda: get_access_token_with_identity(SessionLocal, config, client)
                    for _ in range(5):
                        if not process_pending(SessionLocal, token_provider, client):
                            break
        except Exception as exc:
            logging.error('Error de sincronización Teams (%s); se reintentará', type(exc).__name__)
    return reminders, delivered


def main():
    while True:
        run_once()
        time.sleep(60)


if __name__ == "__main__":
    main()
