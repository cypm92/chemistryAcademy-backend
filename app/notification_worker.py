"""Run the durable notification queue and schedule reminders."""
import logging
import time

from .database import Base, SessionLocal, engine, ensure_notification_lease_column
from .notifications import deliver_pending, enqueue_due_reminders

logging.basicConfig(level=logging.INFO)


def run_once():
    Base.metadata.create_all(engine)
    ensure_notification_lease_column()
    reminders = enqueue_due_reminders(SessionLocal)
    delivered = deliver_pending(SessionLocal)
    return reminders, delivered


def main():
    while True:
        run_once()
        time.sleep(60)


if __name__ == "__main__":
    main()
