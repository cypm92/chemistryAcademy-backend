"""Run only against a disposable PostgreSQL database via ISOLATED_POSTGRES_TEST_URL."""
import os
import threading
import unittest
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.database import Base
from app.main import block_booking_range


@unittest.skipUnless(os.getenv('ISOLATED_POSTGRES_TEST_URL'), 'requires disposable PostgreSQL')
class BookingRaceTest(unittest.TestCase):
    def test_concurrent_overlapping_blocks(self):
        engine = create_engine(os.environ['ISOLATED_POSTGRES_TEST_URL'])
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False)
        start = datetime(2040, 1, 2, 12, tzinfo=timezone.utc) + timedelta(seconds=uuid4().int % 100000000)
        data = schemas.BookingBlockCreate(starts_at=start, ends_at=start + timedelta(hours=1), status='blocked', note='race')
        reached, release = threading.Event(), threading.Event()
        outcomes = []
        def after_sql(conn, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name == 'first' and statement.lstrip().upper().startswith('SELECT') and 'bookings.starts_at <' in statement:
                reached.set()
                if not release.wait(10):
                    raise RuntimeError('barrier timeout')
        event.listen(engine, 'after_cursor_execute', after_sql)
        def run():
            try:
                with sessions() as db:
                    block_booking_range(data, SimpleNamespace(id=1, role='admin'), db)
                outcomes.append(201)
            except HTTPException as exc:
                outcomes.append(exc.status_code)
            except Exception as exc:
                outcomes.append(repr(exc))
        first = threading.Thread(target=run, name='first'); first.start()
        try:
            self.assertTrue(reached.wait(10))
            second = threading.Thread(target=run, name='second'); second.start()
            second.join(1)
        finally:
            release.set()
        first.join(10); second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertCountEqual(outcomes, [201, 409])
        with sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.Booking).where(models.Booking.starts_at == start)))), 1)
        engine.dispose()
