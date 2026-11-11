"""Unwired persistence helpers for eventual Teams synchronization.

Worker for synchronizing Teams meetings.
"""
from datetime import timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .teams_models import TeamsCreationAttempt, TeamsRemoteOperation, TeamsSyncJob, utcnow
from .microsoft_refresh import MicrosoftAccessTokenIdentity
from .microsoft_auth_models import MicrosoftOAuthGeneration


def _identity(provider):
    identity = provider()
    if (not isinstance(identity, MicrosoftAccessTokenIdentity) or not identity.token
            or not identity.tenant_id or not identity.account_object_id
            or not isinstance(identity.oauth_generation, int) or identity.oauth_generation < 1):
        raise ValueError('Microsoft identity unavailable')
    return identity


def _known_event_token(db, job_id, event_id, identity):
    from sqlalchemy import select
    binding = db.scalar(select(MicrosoftOAuthGeneration).where(MicrosoftOAuthGeneration.id == 1))
    if (binding is None or (binding.tenant_id, binding.account_object_id, binding.generation) !=
            (identity.tenant_id, identity.account_object_id, identity.oauth_generation)):
        raise ValueError('Microsoft identity changed')
    attempt = db.scalar(select(TeamsCreationAttempt).where(
        TeamsCreationAttempt.job_id == job_id, TeamsCreationAttempt.event_id == event_id,
        TeamsCreationAttempt.state == 'identified').order_by(TeamsCreationAttempt.id.desc()).limit(1))
    if (attempt is None or (attempt.tenant_id, attempt.account_object_id) !=
            (identity.tenant_id, identity.account_object_id)):
        from .teams_models import TeamsEventOwnership
        owner = TeamsEventOwnership.for_event(identity.tenant_id, identity.account_object_id, event_id, job_id, 'manual_link')
        reserved = db.scalar(select(TeamsEventOwnership).where(
            TeamsEventOwnership.tenant_id == owner.tenant_id,
            TeamsEventOwnership.account_object_id == owner.account_object_id,
            TeamsEventOwnership.event_key == owner.event_key))
        if (reserved is None or reserved.event_id != event_id or reserved.job_id != job_id
                or reserved.provenance != 'manual_link'):
            raise ValueError('Microsoft event identity unavailable or mismatched')
    return identity.token


def merge_teams_comment(existing, old_url, new_url):
    text = existing or ''
    lines = text.split('\n') if text else []
    old_line = f'Enlace de Teams: {old_url}' if old_url else None
    new_line = f'Enlace de Teams: {new_url}' if new_url else None
    if old_line and old_line in lines:
        index = len(lines) - 1 - lines[::-1].index(old_line)
        if new_line:
            lines[index] = new_line
        else:
            lines.pop(index)
    elif new_line:
        lines.append(new_line)
    return '\n'.join(lines)


def enqueue_booking(db, booking, desired):
    """Record desired state in caller's transaction; never performs network I/O."""
    if desired not in ('upsert', 'delete'):
        raise ValueError('desired must be upsert or delete')
    if booking.id is None:
        raise ValueError('booking must have an id')
    user = booking.user
    name = user.name if user else booking.guest_name
    starts = booking.starts_at
    # Application database values represent UTC, even if SQLite drops tzinfo.
    local = (starts.replace(tzinfo=timezone.utc) if starts.tzinfo is None else starts).astimezone(ZoneInfo('Europe/Madrid'))
    subject = f'{name or ""}_{local:%d-%m-%Y %H:%M}'
    from sqlalchemy import case
    table = TeamsSyncJob.__table__
    dialect = db.get_bind().dialect.name
    if dialect not in ('sqlite', 'postgresql'):
        raise NotImplementedError('Only PostgreSQL and SQLite are supported')
    insert = pg_insert if dialect == 'postgresql' else sqlite_insert
    statement = insert(table).values(
        booking_key=f'booking:{booking.id}', booking_id=booking.id,
        revision=1, desired=desired, transaction_id=str(uuid4()),
        subject=subject, starts_at=starts, ends_at=booking.ends_at,
        applied_revision=0, attempts=0, available_at=utcnow(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.booking_key],
        set_={'revision': table.c.revision + 1, 'desired': desired,
              'subject': subject, 'starts_at': starts, 'ends_at': booking.ends_at,
              'available_at': utcnow(), 'attempts': 0,
              'last_error': case((table.c.creation_in_flight.is_(True), 'creation_result_unknown'),
                                 ((table.c.last_error == 'recreation_account_mismatch') & (desired == 'upsert'), 'recreation_account_mismatch'),
                                 ((table.c.last_error == 'event_missing') & (desired == 'upsert'), 'event_missing'), else_=None)},
    )
    db.execute(statement)


def detach_deleted_booking(db, booking):
    """Detach an already-enqueued delete in the caller's transaction.

    Callers must lock the booking first and enqueue its delete if a job exists.
    No job is created for legacy bookings. The job ID remains stable for orphans.
    """
    from sqlalchemy import select

    if booking.id is None:
        raise ValueError('booking must have an id')
    job = db.scalar(select(TeamsSyncJob).where(
        TeamsSyncJob.booking_key == f'booking:{booking.id}'
    ).with_for_update().execution_options(populate_existing=True))
    if job is None:
        return None
    if job.booking_id != booking.id or job.desired != 'delete':
        raise ValueError('booking delete must be enqueued before detaching')
    job.booking_id = None
    job.booking_key = f'deleted:{uuid4()}'
    db.flush()
    return job


def _reconcile_one(session_factory, token_provider, graph_client):
    """Execute one explicitly requested reconciliation, fenced by a lease."""
    from datetime import timedelta
    from sqlalchemy import or_, select
    from . import microsoft_graph as graph
    from .models import Booking

    now, lease = utcnow(), str(uuid4())
    with session_factory.begin() as db:
        job = db.scalar(select(TeamsSyncJob).where(
            TeamsSyncJob.desired == 'upsert', TeamsSyncJob.booking_id.is_not(None),
            TeamsSyncJob.event_id.is_not(None), TeamsSyncJob.join_url.is_not(None),
            TeamsSyncJob.creation_in_flight.is_(False),
            TeamsSyncJob.last_error == 'manual_reconcile',
            TeamsSyncJob.applied_revision == TeamsSyncJob.revision,
            or_(TeamsSyncJob.reconcile_at.is_(None), TeamsSyncJob.reconcile_at <= now),
            or_(TeamsSyncJob.lease_expires_at.is_(None), TeamsSyncJob.lease_expires_at <= now),
        ).order_by(TeamsSyncJob.reconcile_at.asc().nullsfirst(), TeamsSyncJob.id)
          .with_for_update(skip_locked=True).limit(1))
        if job is None:
            return False
        job.lease_token = lease
        job.lease_expires_at = now + timedelta(minutes=5)
        snapshot = (job.id, job.revision, job.event_id, job.subject, job.starts_at, job.ends_at, job.join_url, job.booking_id)
    state = None
    error = None
    try:
        identity = _identity(token_provider)
        with session_factory() as db:
            token = _known_event_token(db, snapshot[0], snapshot[2], identity)
        state = graph.get_event_state(graph_client, token, snapshot[2])
    except Exception as exc:
        error = exc
    with session_factory.begin() as db:
        booking = db.scalar(select(Booking).where(Booking.id == snapshot[7]).with_for_update())
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.id == snapshot[0]).with_for_update())
        if job is None or job.lease_token != lease:
            return True
        job.lease_token = job.lease_expires_at = None
        delay = 900 if state is not None else 300
        if isinstance(error, graph.GraphRequestError) and error.retry_after:
            delay = max(delay, min(86400, int(error.retry_after)))
        job.reconcile_at = utcnow() + timedelta(seconds=delay)
        if job.desired == 'upsert' and job.event_id == snapshot[2]:
            if isinstance(error, graph.GraphRequestError) and error.status_code == 404:
                job.last_error = 'event_missing'
                if booking is not None:
                    booking.admin_comment = merge_teams_comment(booking.admin_comment, job.join_url, None)
            elif error is not None and job.revision == snapshot[1]:
                job.last_error = ('graph_error_%s' % error.status_code if isinstance(error, graph.GraphRequestError) else 'worker_error')
            elif error is None and job.revision == snapshot[1] and job.last_error and job.last_error.startswith(('graph_error_', 'worker_error', 'manual_reconcile')):
                job.last_error = None
        if (state is not None and job.desired == 'upsert' and job.event_id == snapshot[2]
                and job.revision == snapshot[1] and job.applied_revision == job.revision):
            def aware(value):
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            desired = (snapshot[3], aware(snapshot[4]), aware(snapshot[5]), snapshot[6])
            if state != desired:
                job.revision += 1
                job.available_at = utcnow()
    return True


# Each call handles at most one explicitly requested check and one pending job.
def process_pending(session_factory, token_provider, graph_client):
    """Claim one due revision, release DB locks for I/O, then reconcile it.

    token_provider is a zero-argument callable. Returns True if a job was claimed.
    Remote effects are not exactly-once: a crash after Graph accepts a PATCH but
    before its reconciliation transaction commits can leave an undetected stale
    remote event. Successfully synchronized events are not checked periodically.
    """
    from datetime import timedelta
    from sqlalchemy import or_, select, update
    from . import microsoft_graph as graph
    from .models import Booking

    reconciled = _reconcile_one(session_factory, token_provider, graph_client)
    # Orphan records are evidence, not authorization for automatic deletion.
    now = utcnow()
    lease = str(uuid4())
    with session_factory.begin() as db:
        candidate = (select(TeamsSyncJob.id)
            .where(TeamsSyncJob.applied_revision < TeamsSyncJob.revision,
                   TeamsSyncJob.available_at <= now,
                   or_(TeamsSyncJob.desired == 'delete', TeamsSyncJob.last_error.is_(None), TeamsSyncJob.last_error != 'event_missing'),
                   or_(TeamsSyncJob.desired == 'delete', TeamsSyncJob.last_error.is_(None), TeamsSyncJob.last_error != 'recreation_account_mismatch'),
                   or_(TeamsSyncJob.creation_in_flight.is_(False),
                       TeamsSyncJob.last_error.is_(None),
                       TeamsSyncJob.last_error != 'creation_result_unknown'),
                   or_(TeamsSyncJob.desired == 'delete', TeamsSyncJob.event_id.is_not(None),
                       ~select(TeamsCreationAttempt.id).where(
                           TeamsCreationAttempt.job_id == TeamsSyncJob.id,
                           TeamsCreationAttempt.needs_review.is_(True)).exists(),
                       (TeamsSyncJob.recreation_transaction_id == TeamsSyncJob.transaction_id) &
                       TeamsSyncJob.recreation_at.is_not(None)),
                   or_(TeamsSyncJob.lease_expires_at.is_(None), TeamsSyncJob.lease_expires_at <= now))
            .order_by(TeamsSyncJob.available_at, TeamsSyncJob.id)
            .with_for_update(skip_locked=True).limit(1).scalar_subquery())
        claimed = db.execute(update(TeamsSyncJob).where(
            TeamsSyncJob.id == candidate,
            or_(TeamsSyncJob.lease_expires_at.is_(None), TeamsSyncJob.lease_expires_at <= now))
            .values(lease_token=lease, lease_expires_at=now + timedelta(minutes=5))
            .returning(TeamsSyncJob.id)).scalar_one_or_none()
        if claimed is None:
            return reconciled
        job = db.get(TeamsSyncJob, claimed)
        if job.recreation_transaction_id == job.transaction_id and job.desired == 'upsert' and job.event_id is None:
            binding = db.get(MicrosoftOAuthGeneration, 1)
            if (not job.recreation_at or not job.recreation_admin_id or job.recreation_revision is None or
                    job.recreation_reason not in ('creation_result_unknown', 'event_missing') or
                    not job.recreation_tenant_id or not job.recreation_account_id or binding is None or
                    (job.recreation_tenant_id, job.recreation_account_id) != (binding.tenant_id, binding.account_object_id)):
                job.last_error = 'recreation_account_mismatch'
                job.lease_token = job.lease_expires_at = None
                return True
        if job.creation_in_flight:
            job.last_error = 'creation_result_unknown'
            job.lease_token = job.lease_expires_at = None
            return True
        snapshot = {name: getattr(job, name) for name in (
            'id', 'booking_id', 'revision', 'desired', 'transaction_id', 'subject', 'starts_at', 'ends_at', 'event_id', 'join_url')}


    error = None
    event_id, join_url = snapshot['event_id'], None
    created = False
    creation_started = False
    attempt_id = None
    patch_attempted = False
    operation_id = None
    try:
        if snapshot['desired'] == 'delete' and not event_id:
            pass
        else:
            identity = _identity(token_provider)
            token = identity.token
            if event_id:
                with session_factory() as db:
                    token = _known_event_token(db, claimed, event_id, identity)
            if snapshot['desired'] == 'delete':
                try:
                    graph.delete_event(graph_client, token, event_id)
                except graph.GraphRequestError as exc:
                    if exc.status_code != 404:
                        raise
            else:
                def aware(value):
                    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
                if event_id:
                    def patch_event():
                        nonlocal operation_id, patch_attempted
                        # Serialize with physical deletion and identity changes. The
                        # marker must commit before the remote request can start.
                        with session_factory.begin() as db:
                            binding = db.scalar(select(MicrosoftOAuthGeneration).where(
                                MicrosoftOAuthGeneration.id == 1).with_for_update())
                            current = db.scalar(select(TeamsSyncJob).where(
                                TeamsSyncJob.id == claimed).with_for_update())
                            if (binding is None or (binding.tenant_id, binding.account_object_id, binding.generation) !=
                                    (identity.tenant_id, identity.account_object_id, identity.oauth_generation)
                                    or current is None or current.lease_token != lease
                                    or current.lease_expires_at is None or aware(current.lease_expires_at) <= utcnow()
                                    or current.revision != snapshot['revision'] or current.desired != 'upsert'
                                    or current.event_id != event_id or current.booking_id != snapshot['booking_id']):
                                return None, False
                            _known_event_token(db, claimed, event_id, identity)
                            operation_id = str(uuid4())
                            db.add(TeamsRemoteOperation(id=operation_id, job_id=claimed,
                                operation='patch', state='in_flight', event_id=event_id,
                                transaction_revision=snapshot['revision'], tenant_id=identity.tenant_id,
                                account_object_id=identity.account_object_id, oauth_generation=identity.oauth_generation))
                        patch_attempted = True
                        return graph.update_event(graph_client, token, event_id, snapshot['subject'], aware(snapshot['starts_at']), aware(snapshot['ends_at'])), True
                    try:
                        if snapshot['join_url'] is None:
                            join_url = graph.get_event(graph_client, token, event_id)
                            if join_url:
                                patched_url, sent = patch_event()
                                if not sent:
                                    return True
                                join_url = patched_url or join_url
                        else:
                            join_url, sent = patch_event()
                            if not sent:
                                return True
                    except graph.GraphRequestError as exc:
                        raise
                else:
                    # Commit uncertainty before POST; never hold a row lock over I/O.
                    with session_factory.begin() as db:
                        binding = db.scalar(select(MicrosoftOAuthGeneration).where(
                            MicrosoftOAuthGeneration.id == 1).with_for_update())
                        if (binding is None or (binding.tenant_id, binding.account_object_id, binding.generation) !=
                                (identity.tenant_id, identity.account_object_id, identity.oauth_generation)):
                            raise ValueError('Microsoft identity changed')
                        current = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.id == claimed).with_for_update())
                        if (current is None or current.lease_token != lease or
                                current.revision != snapshot['revision'] or current.creation_in_flight
                                or current.event_id is not None or current.transaction_id != snapshot['transaction_id']):
                            return True
                        # Only an explicit rejection permits another POST. Reuse the
                        # durable transaction record, never an ambiguous attempt.
                        if current.recreation_transaction_id == current.transaction_id and (not current.recreation_at or
                                not current.recreation_admin_id or current.recreation_revision is None or
                                current.recreation_reason not in ('creation_result_unknown', 'event_missing') or
                                not current.recreation_tenant_id or not current.recreation_account_id or
                                (current.recreation_tenant_id, current.recreation_account_id) !=
                                (identity.tenant_id, identity.account_object_id)):
                            current.last_error = 'recreation_account_mismatch'
                            current.lease_token = current.lease_expires_at = None
                            return True
                        attempt = db.scalar(select(TeamsCreationAttempt).where(
                            TeamsCreationAttempt.transaction_id == current.transaction_id).with_for_update())
                        if attempt is not None:
                            if (attempt.state != 'rejected' or attempt.job_id != claimed or
                                    (attempt.authorized_at is not None and attempt.dispatched_at is None) or
                                    (attempt.tenant_id, attempt.account_object_id) !=
                                    (identity.tenant_id, identity.account_object_id)):
                                current.last_error = 'creation_result_unknown'
                                current.creation_in_flight = True
                                current.lease_token = current.lease_expires_at = None
                                return True
                            attempt.state = 'in_flight'
                            attempt.dispatched_at = utcnow()
                            attempt.revision = current.revision
                            attempt.oauth_generation = identity.oauth_generation
                        else:
                            attempt = TeamsCreationAttempt(job_id=claimed, transaction_id=current.transaction_id,
                                revision=current.revision, tenant_id=identity.tenant_id,
                                account_object_id=identity.account_object_id,
                                oauth_generation=identity.oauth_generation, state='in_flight')
                            db.add(attempt)
                        db.flush()
                        attempt_id = attempt.id
                        current.creation_in_flight = True
                    creation_started = True
                    event_id, join_url = graph.create_event(graph_client, token, snapshot['subject'], aware(snapshot['starts_at']), aware(snapshot['ends_at']), snapshot['transaction_id'])
                    created = True
    except Exception as exc:
        # Never persist exception text: providers may include credentials in it.
        error = exc

    with session_factory.begin() as db:
        # Booking writers acquire this row before enqueueing (and locking) the job.
        # Preserve that order to avoid deadlocks and lost comment updates.
        booking = db.scalar(select(Booking).where(Booking.id == snapshot['booking_id']).with_for_update()) if snapshot['booking_id'] is not None else None
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.id == claimed).with_for_update())
        if operation_id is not None:
            operation = db.scalar(select(TeamsRemoteOperation).where(
                TeamsRemoteOperation.id == operation_id).with_for_update())
            if operation is not None:
                if operation.reviewed_at is not None:
                    operation.reviewed_at = None
                    # New evidence reopens the original risk; it does not
                    # authorize changing a newer job, even after deletion.
                    operation.state = 'unknown'
                    operation.completed_at = None
                    return True
                definite = (error is None or isinstance(error, graph.GraphRequestError)
                            and error.status_code is not None
                            and (200 <= error.status_code < 300 or
                                 400 <= error.status_code < 500 and error.status_code != 408))
                operation.state = 'completed' if definite else 'unknown'
                operation.completed_at = utcnow() if definite else None
        attempt = None
        if attempt_id is not None:
            attempt = db.scalar(select(TeamsCreationAttempt).where(TeamsCreationAttempt.id == attempt_id).with_for_update())
            if attempt is not None:
                if created:
                    attempt.state, attempt.event_id, attempt.join_url = 'identified', event_id, join_url
                elif isinstance(error, graph.GraphRequestError) and error.status_code in (401, 403, 429):
                    attempt.state = 'rejected'
                else:
                    attempt.state = 'unknown'
        if job is None:
            return True
        # An older callback is evidence only, not authority to alter a newer selection.
        if creation_started and job.transaction_id != snapshot['transaction_id']:
            if created and job.event_id != event_id:
                if attempt is not None:
                    attempt.needs_review = True

            return True
        # Only these explicit POST rejections prove no event was created.
        # Transport failures, 5xx, malformed success and other statuses remain
        # unknown. Token failures occur before the marker and retry normally.
        if (creation_started and isinstance(error, graph.GraphRequestError) and
                error.status_code in (401, 403, 429) and
                job.transaction_id == snapshot['transaction_id']):
            job.creation_in_flight = False
            if job.last_error == 'creation_result_unknown':
                job.last_error = None
        # Persist every distinct creation result before checking lease ownership.
        review = db.scalar(select(TeamsCreationAttempt.id).where(
            TeamsCreationAttempt.job_id == job.id, TeamsCreationAttempt.event_id == event_id,
            TeamsCreationAttempt.needs_review.is_(True)).limit(1)) if created else None
        if created and review is None and (not job.event_id or job.event_id == event_id):
            job.event_id = event_id
            job.creation_in_flight = False
            # A delete may already have been acknowledged while POST was in
            # flight. Its result is a new remote side effect to reconcile.
            if job.desired == 'delete' and job.applied_revision >= job.revision:
                job.applied_revision = job.revision - 1
                job.available_at = utcnow()
        elif created:
            if attempt is not None:
                attempt.needs_review = True
            if not job.event_id:
                job.transaction_id = str(uuid4())
                if job.desired == 'upsert' and job.applied_revision >= job.revision:
                    job.applied_revision = job.revision - 1
            if job.lease_token == lease:
                job.lease_token = job.lease_expires_at = None
                job.available_at = utcnow()
            return True
        if (patch_attempted and job.event_id == snapshot['event_id'] and
                (job.lease_token != lease or job.revision != snapshot['revision'])):
            # A late PATCH (even an ambiguous failure) may have overwritten a
            # newer remote state. Advance the generation without changing the
            # applied revision, identity, URL or an active worker's lease. That
            # worker is fenced by the generation check and must reconcile again.
            # A completed DELETE has cleared event_id, so it is never resurrected.
            job.revision += 1
            job.available_at = utcnow()
        if job.lease_token != lease:
            return True
        job.lease_token = None
        job.lease_expires_at = None
        if job.creation_in_flight:
            job.last_error = 'creation_result_unknown'
            return True
        if (job.desired == 'upsert' and job.event_id == snapshot['event_id'] and snapshot['event_id'] and
                isinstance(error, graph.GraphRequestError) and error.status_code == 404):
            job.last_error = 'event_missing'
            if booking is not None:
                booking.admin_comment = merge_teams_comment(booking.admin_comment, job.join_url, None)
            return True
        if job.revision != snapshot['revision']:
            job.available_at = utcnow()
            return True
        if error is not None:
            job.attempts += 1
            delay = min(3600, 30 * (2 ** min(job.attempts - 1, 7)))
            if isinstance(error, graph.GraphRequestError) and error.retry_after:
                delay = max(delay, min(86400, int(error.retry_after)))
            job.available_at = utcnow() + timedelta(seconds=delay)
            job.last_error = ('graph_error_%s' % error.status_code if isinstance(error, graph.GraphRequestError) else 'worker_error')
            return True
        if snapshot['desired'] == 'upsert' and not event_id:
            job.event_id = None
            job.transaction_id = str(uuid4())
            job.available_at = utcnow()
            return True
        if snapshot['desired'] == 'delete':
            if booking is not None:
                booking.admin_comment = merge_teams_comment(booking.admin_comment, job.join_url, None)
            job.event_id = None
            job.join_url = None
            job.transaction_id = str(uuid4())
        else:
            new_url = join_url or job.join_url
            if not new_url:
                # The remote event already exists. Keep its identity and retry
                # rather than marking the booking ready or issuing another POST.
                job.attempts += 1
                job.last_error = 'missing_join_url'
                job.available_at = utcnow() + timedelta(seconds=30)
                return True
            if booking is not None and booking.status == 'confirmed' and job.desired == 'upsert':
                booking.admin_comment = merge_teams_comment(booking.admin_comment, job.join_url, new_url)
            job.join_url = new_url
        job.applied_revision = snapshot['revision']
        job.attempts = 0
        job.last_error = None
    return True
