"""Durable Teams synchronization state. Registration is independent of routes."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


class TeamsSyncJob(Base):
    __tablename__ = 'teams_sync_jobs'

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # Deliberately no FK: the tombstone must survive physical booking deletion.
    booking_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    desired: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    creation_in_flight: Mapped[bool] = mapped_column(nullable=False, default=False, server_default='0')
    join_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    recreation_transaction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recreation_admin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recreation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recreation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recreation_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recreation_tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recreation_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recreation_prior_transaction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recreation_prior_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    recreation_prior_join_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class TeamsCreationAttempt(Base):
    __tablename__ = 'teams_creation_attempts'
    __table_args__ = (
        CheckConstraint("state IN ('in_flight', 'unknown', 'identified', 'rejected')", name='ck_teams_creation_attempt_state'),
        Index('ix_teams_creation_attempt_job_state', 'job_id', 'state'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # No booking FK: attempts remain available after the booking is deleted.
    job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    oauth_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    # Legacy authorization fields are retained as historical data only.
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text('false'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TeamsRemoteOperation(Base):
    """Durable record of a remote PATCH, independent of the job lifecycle."""
    __tablename__ = 'teams_remote_operations'
    __table_args__ = (
        CheckConstraint("operation = 'patch'", name='ck_teams_remote_operation_type'),
        CheckConstraint("state IN ('in_flight', 'unknown', 'completed')", name='ck_teams_remote_operation_state'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # No FK: survives tombstones.
    operation: Mapped[str] = mapped_column(String(16), nullable=False, default='patch')
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    oauth_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Active administrator acceptance; it does not change the remote result.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_desired: Mapped[str | None] = mapped_column(String(10), nullable=True)


class TeamsEventOwnership(Base):
    """Permanent reservation; callers must never delete or reassign a row.

    On a unique conflict, compare the full event_id before treating it as the
    same event; a hash collision is a hard conflict, not an existing match.
    """
    __tablename__ = 'teams_event_ownership'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'account_object_id', 'event_key', name='uq_teams_event_owner'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    # No FK: records survive job/booking deletion and remain reserved forever.
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    provenance: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    @classmethod
    def for_event(cls, tenant_id, account_object_id, event_id, job_id, provenance):
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('event_id must be a non-empty string')
        return cls(tenant_id=str(UUID(str(tenant_id))),
                   account_object_id=str(UUID(str(account_object_id))),
                   event_key=sha256(event_id.encode('utf-8')).hexdigest(),
                   event_id=event_id, job_id=job_id, provenance=provenance)
