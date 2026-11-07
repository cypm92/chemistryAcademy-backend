"""Regression: an accepted POST with a lost response must not orphan an event."""
from teams_identity_fixtures import IDENTITY, seed_account, seed_identified_event
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models  # register booking table
from app.teams_models import TeamsSyncJob
from app.teams_sync import enqueue_booking, process_pending


def test_cancel_after_accepted_post_with_lost_response_cleans_remote_event():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    booking = SimpleNamespace(id=912, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                              ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc),
                              guest_name='Ana', guest_email='a@example.org', user=None)
    remote = {}
    calls = []

    def graph(request):
        calls.append(request.method)
        if request.method == 'POST':
            payload = json.loads(request.content)
            key = payload['transactionId']
            if key not in remote:
                remote[key] = 'remote-event'
                raise httpx.ReadTimeout('response lost', request=request)
            return httpx.Response(201, json={'id': remote[key]})
        assert request.method == 'DELETE'
        assert request.url.path.endswith('/remote-event')
        remote.clear()
        return httpx.Response(204)

    with sessions.begin() as db:
        seed_account(db)
        enqueue_booking(db, booking, 'upsert')
    with httpx.Client(transport=httpx.MockTransport(graph)) as client:
        assert process_pending(sessions, lambda: IDENTITY, client)
        assert remote
        with sessions.begin() as db:
            enqueue_booking(db, booking, 'delete')
        for _ in range(4):
            if not process_pending(sessions, lambda: IDENTITY, client):
                break
    with sessions() as db:
        job = db.scalar(select(TeamsSyncJob))
        assert job.applied_revision < job.revision
        assert job.last_error == 'creation_result_unknown'
    assert remote
    assert calls == ['POST']
