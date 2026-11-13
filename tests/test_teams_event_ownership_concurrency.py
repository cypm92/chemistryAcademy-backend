
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from ephemeral_postgres import postgres_test_db
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import ensure_application_schema
from app.teams_models import TeamsEventOwnership as Ownership

TENANT = 'aaaaaaaa-0000-0000-0000-000000000001'
ACCOUNT = 'bbbbbbbb-0000-0000-0000-000000000002'

@pytest.fixture
def engine(postgres_test_db):
    return postgres_test_db.engine


def test_legacy_durability(engine):
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE teams_sync_jobs (id INTEGER PRIMARY KEY, transaction_id VARCHAR(36), revision INTEGER, creation_in_flight BOOLEAN)'))
        conn.execute(text("INSERT INTO teams_sync_jobs VALUES (1, 'legacy', 1, FALSE)"))
    ensure_application_schema(engine)
    ensure_application_schema(engine)
    with Session(engine) as session:
        assert session.query(Ownership).count() == 0
        session.add(Ownership.for_event(TENANT.upper(), ACCOUNT.upper(), 'Event', 1, 'manual'))
        session.commit()
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM teams_sync_jobs'))
    with pytest.raises(IntegrityError):
        with Session(engine) as session:
            session.add(Ownership.for_event(TENANT, ACCOUNT, 'Event', 2, 'manual'))
            session.commit()
    with Session(engine) as session:
        row = session.query(Ownership).one()
        assert (row.tenant_id, row.account_object_id, row.event_id, row.job_id, row.provenance) == (TENANT, ACCOUNT, 'Event', 1, 'manual')
        assert row.created_at is not None


def test_distinct_identities_case_long_id_and_collision(engine):
    ensure_application_schema(engine)
    event = 'E' * 20000
    with Session(engine) as session:
        session.add_all([Ownership.for_event(TENANT, ACCOUNT, event, 1, 'manual'),
                         Ownership.for_event(TENANT, ACCOUNT, event.lower(), 2, 'manual'),
                         Ownership.for_event(str(uuid4()), ACCOUNT, event, 3, 'manual'),
                         Ownership.for_event(TENANT, str(uuid4()), event, 4, 'manual')])
        session.commit()
    collision = Ownership.for_event(TENANT, ACCOUNT, 'different event', 5, 'manual')
    collision.event_key = Ownership.for_event(TENANT, ACCOUNT, event, 1, 'manual').event_key
    with pytest.raises(IntegrityError):
        with Session(engine) as session:
            session.add(collision)
            session.commit()
    with Session(engine) as session:
        existing = session.query(Ownership).filter_by(tenant_id=TENANT, account_object_id=ACCOUNT, event_key=collision.event_key).one()
        assert existing.event_id != 'different event'
        assert session.query(Ownership).count() == 4


def test_concurrent_claims_have_exactly_one_winner(engine):
    ensure_application_schema(engine)
    barrier = Barrier(2)
    def claim(job):
        with Session(engine) as session:
            session.add(Ownership.for_event(TENANT, ACCOUNT, 'same', job, 'manual'))
            barrier.wait(timeout=10)
            try:
                session.commit()
                return 'won'
            except IntegrityError:
                session.rollback()
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (1, 2))) == ['conflict', 'won']
    with Session(engine) as session:
        assert session.query(Ownership).count() == 1
