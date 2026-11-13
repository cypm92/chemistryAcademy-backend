from unittest.mock import MagicMock, Mock, call
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import inspect, text
from ephemeral_postgres import postgres_test_db
from sqlalchemy.orm import sessionmaker

from app import database, models, notifications
from app import notification_worker as worker


def test_worker_migrates_microsoft_schema_before_queries(monkeypatch):
    steps = Mock()
    monkeypatch.setattr(worker, 'ensure_application_schema', steps.schema, raising=False)
    monkeypatch.setattr(worker, 'ensure_notification_lease_column', steps.lease)
    monkeypatch.setattr(worker, 'ensure_booking_management_column', steps.booking)
    monkeypatch.setattr(worker, 'enqueue_due_reports', steps.reports)
    monkeypatch.setattr(worker, 'enqueue_due_reminders', steps.reminders)
    monkeypatch.setattr(worker, 'deliver_pending', steps.deliver)

    worker.run_once()

    assert steps.mock_calls == [
        call.schema(worker.engine), call.lease(),
        call.booking(), call.reports(worker.SessionLocal),
        call.reminders(worker.SessionLocal), call.deliver(worker.SessionLocal),
    ]


def test_worker_migrates_legacy_schema_and_processes_mail_without_microsoft(monkeypatch, postgres_test_db):
    engine = postgres_test_db.engine
    factory = sessionmaker(bind=engine, autoflush=False)
    database.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE microsoft_oauth_generation'))
        conn.execute(text('CREATE TABLE microsoft_oauth_generation (id INTEGER PRIMARY KEY, generation INTEGER NOT NULL)'))
        conn.execute(text('INSERT INTO microsoft_oauth_generation VALUES (1, 3)'))
    with factory() as db:
        notifications.enqueue(db, 'contact:1', 'user@example.test', 'Subject', 'Body')
        db.commit()
    monkeypatch.setattr(worker, 'engine', engine)
    monkeypatch.setattr(worker, 'SessionLocal', factory)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(worker, 'ensure_booking_management_column', lambda: database.ensure_booking_management_column(engine))
    monkeypatch.setattr(notifications.settings, 'smtp_mode', 'log')
    for name in ('MICROSOFT_CLIENT_ID', 'MICROSOFT_CLIENT_SECRET', 'MICROSOFT_TENANT_ID'):
        monkeypatch.delenv(name, raising=False)

    worker.run_once()

    assert {'account_object_id', 'tenant_id'} <= {c['name'] for c in inspect(engine).get_columns('microsoft_oauth_generation')}
    with factory() as db:
        assert db.query(models.NotificationEvent).filter_by(logical_key='contact:1').one().status == 'simulated'
    engine.dispose()


def test_enabled_worker_processes_teams_after_mail_with_bounded_batch(monkeypatch):
    steps = Mock()
    for name in ('ensure_application_schema', 'ensure_notification_lease_column',
                 'ensure_booking_management_column', 'enqueue_due_reports',
                 'enqueue_due_reminders', 'deliver_pending'):
        monkeypatch.setattr(worker, name, getattr(steps, name))
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'true')
    config = object()
    monkeypatch.setattr(worker, 'get_config', lambda: config, raising=False)
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(httpx, 'Client', steps.client)
    steps.client.return_value = client
    monkeypatch.setattr(worker, 'get_access_token_with_identity', steps.token, raising=False)
    def process(factory, token_provider, graph_client):
        steps.process(factory, graph_client, token_provider())
        return True
    monkeypatch.setattr(worker, 'process_pending', process, raising=False)
    worker.run_once()
    assert steps.mock_calls.index(call.deliver_pending(worker.SessionLocal)) < steps.mock_calls.index(call.client(timeout=10.0, follow_redirects=False))
    assert steps.process.call_count == 5
    assert steps.token.call_count == 5
    steps.token.assert_called_with(worker.SessionLocal, config, client)


@pytest.mark.parametrize('flag', [None, 'false', '1', 'yes'])
def test_disabled_never_constructs_http_client(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv('MICROSOFT_SYNC_ENABLED', raising=False)
    else:
        monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', flag)
    for name in ('ensure_application_schema', 'ensure_notification_lease_column', 'ensure_booking_management_column',
                 'enqueue_due_reports', 'enqueue_due_reminders', 'deliver_pending'):
        monkeypatch.setattr(worker, name, Mock())
    config = Mock(side_effect=AssertionError('config should not be read'))
    client = Mock(side_effect=AssertionError('HTTP forbidden'))
    monkeypatch.setattr(worker, 'get_config', config)
    monkeypatch.setattr(httpx, 'Client', client)
    worker.run_once()
    config.assert_not_called()
    client.assert_not_called()


def test_invalid_config_never_constructs_http_client(monkeypatch):
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'true')
    for name in ('ensure_application_schema', 'ensure_notification_lease_column', 'ensure_booking_management_column',
                 'enqueue_due_reports', 'enqueue_due_reminders', 'deliver_pending'):
        monkeypatch.setattr(worker, name, Mock())
    monkeypatch.setattr(worker, 'get_config', lambda: None)
    client = Mock(side_effect=AssertionError('HTTP forbidden'))
    monkeypatch.setattr(httpx, 'Client', client)
    worker.run_once()
    client.assert_not_called()


def test_teams_exception_is_isolated_and_sanitized(monkeypatch, caplog):
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'true')
    for name in ('ensure_application_schema', 'ensure_notification_lease_column', 'ensure_booking_management_column',
                 'enqueue_due_reports', 'enqueue_due_reminders', 'deliver_pending'):
        monkeypatch.setattr(worker, name, Mock())
    monkeypatch.setattr(worker, 'get_config', lambda: object())
    monkeypatch.setattr(httpx, 'Client', MagicMock())
    monkeypatch.setattr(worker, 'process_pending', Mock(side_effect=RuntimeError('secret-token')))
    result = worker.run_once()
    assert result == (worker.enqueue_due_reminders.return_value, worker.deliver_pending.return_value)
    assert 'RuntimeError' in caplog.text and 'secret-token' not in caplog.text


@pytest.mark.parametrize('failure', ['reconnect', 'graph'])
def test_teams_failure_persists_retry_after_mail(monkeypatch, failure, postgres_test_db):
    from app.teams_models import TeamsSyncJob
    from app.teams_sync import enqueue_booking
    from app.microsoft_refresh import MicrosoftReconnectRequired
    engine = postgres_test_db.engine
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    booking = SimpleNamespace(id=42, starts_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                              ends_at=datetime(2026, 1, 1, 13, tzinfo=timezone.utc),
                              guest_name='Ana', guest_email='a@example.test', user=None)
    from app.microsoft_auth_models import MicrosoftOAuthGeneration
    from app.microsoft_refresh import MicrosoftAccessTokenIdentity
    with factory.begin() as db:
        db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))
        enqueue_booking(db, booking, 'upsert')
    monkeypatch.setattr(worker, 'SessionLocal', factory)
    for name in ('ensure_application_schema', 'ensure_notification_lease_column', 'ensure_booking_management_column',
                 'enqueue_due_reports', 'enqueue_due_reminders', 'deliver_pending'):
        monkeypatch.setattr(worker, name, Mock())
    monkeypatch.setenv('MICROSOFT_SYNC_ENABLED', 'true')
    monkeypatch.setattr(worker, 'get_config', lambda: object())
    monkeypatch.setattr(httpx, 'Client', MagicMock())
    def fail(*args):
        assert worker.deliver_pending.called
        if failure == 'reconnect':
            raise MicrosoftReconnectRequired('secret-token')
        raise RuntimeError('graph secret-token')
    if failure == 'reconnect':
        monkeypatch.setattr(worker, 'get_access_token_with_identity', fail)
    else:
        from app import microsoft_graph
        monkeypatch.setattr(worker, 'get_access_token_with_identity', lambda *args: MicrosoftAccessTokenIdentity('fake-token', 'tenant', 'account', 4))
        monkeypatch.setattr(microsoft_graph, 'create_event', fail)
    worker.run_once()
    with factory() as db:
        job = db.query(TeamsSyncJob).one()
        if failure == 'reconnect':
            assert job.attempts == 1 and job.last_error == 'worker_error'
            assert not job.creation_in_flight
        else:
            # An exception during creation cannot establish whether POST happened.
            assert job.last_error == 'creation_result_unknown'
            assert job.creation_in_flight
        assert job.applied_revision < job.revision and job.lease_token is None
    engine.dispose()


def test_worker_stops_before_queries_if_migration_fails(monkeypatch):
    steps = Mock()
    monkeypatch.setattr(worker, 'ensure_application_schema', steps.schema)
    monkeypatch.setattr(worker, 'ensure_notification_lease_column', steps.lease)
    monkeypatch.setattr(worker, 'ensure_booking_management_column', steps.booking)
    monkeypatch.setattr(worker, 'enqueue_due_reports', steps.reports)
    monkeypatch.setattr(worker, 'enqueue_due_reminders', steps.reminders)
    monkeypatch.setattr(worker, 'deliver_pending', steps.deliver)
    steps.schema.side_effect = RuntimeError('unsafe schema')
    with pytest.raises(RuntimeError, match='unsafe schema'):
        worker.run_once()
    assert steps.mock_calls == [call.schema(worker.engine)]
