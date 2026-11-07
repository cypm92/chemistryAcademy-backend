"""Admin-only authorization for recreating a Teams event."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from sqlalchemy.exc import IntegrityError

from . import microsoft_oauth_routes
from .microsoft_auth_models import MicrosoftCredential, MicrosoftOAuthGeneration
from .models import Booking
from .security import admin_user
from .teams_models import (TeamsSyncJob, TeamsCreationAttempt, TeamsEventOwnership,
                           TeamsRemoteOperation)
from .teams_sync import merge_teams_comment

router = APIRouter()


@router.get('/admin/teams/unlinked-incidents')
def unlinked_incidents(admin=Depends(admin_user), db: Session = Depends(get_db)):
    patch = select(TeamsRemoteOperation.id).where(
        TeamsRemoteOperation.job_id == TeamsSyncJob.id,
        TeamsRemoteOperation.operation == 'patch',
        or_(TeamsRemoteOperation.state == 'in_flight',
            and_(TeamsRemoteOperation.state == 'unknown', TeamsRemoteOperation.reviewed_at.is_(None))),
    ).exists()
    creation = select(TeamsCreationAttempt.id).where(
        TeamsCreationAttempt.job_id == TeamsSyncJob.id,
        TeamsCreationAttempt.state.in_(('in_flight', 'unknown')),
    ).exists()
    orphan = select(TeamsCreationAttempt.id).where(
        TeamsCreationAttempt.job_id == TeamsSyncJob.id,
        TeamsCreationAttempt.needs_review.is_(True),
    ).exists()
    deletion = and_(TeamsSyncJob.desired == 'delete',
                    or_(TeamsSyncJob.applied_revision < TeamsSyncJob.revision,
                        TeamsSyncJob.last_error.is_not(None)))
    reason = case((patch, 'patch_unresolved'), (creation, 'creation_unresolved'),
                  (orphan, 'orphan_unresolved'), (deletion, 'delete_unresolved'))
    rows = db.execute(select(TeamsSyncJob.id, TeamsSyncJob.starts_at, reason.label('reason'),
                             func.count().over().label('total'))
                      .where(TeamsSyncJob.booking_id.is_(None), or_(patch, creation, orphan, deletion))
                      .order_by(TeamsSyncJob.id).limit(100)).all()
    return JSONResponse({'count': rows[0].total if rows else 0,
            'incidents': [{'job_id': row.id,
                           'starts_at': row.starts_at.isoformat() if row.starts_at else None,
                           'reason': row.reason} for row in rows]},
                        headers=microsoft_oauth_routes.HEADERS)


class RecreateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int
    confirm_absent: Literal[True]

    @field_validator('expected_revision', mode='before')
    @classmethod
    def revision_is_integer(cls, value):
        if type(value) is not int:
            raise ValueError('revision must be an integer')
        return value

    @field_validator('confirm_absent', mode='before')
    @classmethod
    def confirmation_is_true(cls, value):
        if value is not True:
            raise ValueError('confirmation required')
        return value


@router.post('/admin/classes/{booking_id}/teams/recreate')
def recreate(booking_id: int, body: RecreateBody, admin=Depends(admin_user),
             factory=Depends(microsoft_oauth_routes.get_session_factory)):
    now = datetime.now(timezone.utc)
    with factory.begin() as db:
        booking = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
        generation = db.scalar(select(MicrosoftOAuthGeneration).where(MicrosoftOAuthGeneration.id == 1).with_for_update())
        job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id).with_for_update())
        credential = db.scalar(select(MicrosoftCredential).where(MicrosoftCredential.id == 1).with_for_update())
        if not booking or not job:
            raise HTTPException(404, 'Clase no encontrada')
        if (booking.status != 'confirmed' or job.desired != 'upsert'
                or job.last_error not in ('creation_result_unknown', 'event_missing')
                or job.revision != body.expected_revision
                or (job.lease_token and not job.lease_expires_at)
                or (job.lease_expires_at and _utc(job.lease_expires_at) > now)):
            raise HTTPException(409, 'La clase requiere revisión adicional')
        if (not generation or not credential or credential.needs_reconnect
                or not generation.tenant_id or not generation.account_object_id
                or credential.account_object_id != generation.account_object_id):
            raise HTTPException(409, 'La cuenta Microsoft no está conectada')
        try:
            UUID(generation.tenant_id); UUID(generation.account_object_id)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(409, 'La identidad Microsoft no es verificable') from None
        pending = list(db.scalars(select(TeamsCreationAttempt).where(
            TeamsCreationAttempt.job_id == job.id,
            TeamsCreationAttempt.state.in_(('unknown', 'in_flight')))))
        if (job.last_error == 'creation_result_unknown' and not any(
                a.transaction_id == job.transaction_id for a in pending)):
            raise HTTPException(409, 'El intento anterior no es verificable')
        if any((a.tenant_id, a.account_object_id) != (generation.tenant_id, generation.account_object_id)
               or not a.oauth_generation for a in pending):
            raise HTTPException(409, 'La identidad del intento no es verificable; contacta con soporte')
        if job.last_error == 'event_missing' and not job.event_id:
            raise HTTPException(409, 'La identidad del evento no es verificable; contacta con soporte')
        if job.event_id:
            identified = db.scalar(select(TeamsCreationAttempt.id).where(
                TeamsCreationAttempt.job_id == job.id, TeamsCreationAttempt.event_id == job.event_id,
                TeamsCreationAttempt.state == 'identified', TeamsCreationAttempt.tenant_id == generation.tenant_id,
                TeamsCreationAttempt.account_object_id == generation.account_object_id).limit(1))
            owner = TeamsEventOwnership.for_event(generation.tenant_id, generation.account_object_id, job.event_id, job.id, 'manual_link')
            reserved = db.scalar(select(TeamsEventOwnership).where(
                TeamsEventOwnership.tenant_id == owner.tenant_id,
                TeamsEventOwnership.account_object_id == owner.account_object_id,
                TeamsEventOwnership.event_key == owner.event_key))
            if not identified and not (reserved and reserved.event_id == job.event_id and reserved.job_id == job.id and reserved.provenance == 'manual_link'):
                raise HTTPException(409, 'La identidad del evento no es verificable')
        replacement = str(uuid4())
        job.recreation_transaction_id = replacement
        job.recreation_admin_id = admin.id
        job.recreation_at = now
        job.recreation_revision = job.revision
        job.recreation_reason = job.last_error
        job.recreation_prior_transaction_id = job.transaction_id
        job.recreation_prior_event_id, job.recreation_prior_join_url = job.event_id, job.join_url
        job.recreation_tenant_id, job.recreation_account_id = generation.tenant_id, generation.account_object_id
        booking.admin_comment = merge_teams_comment(booking.admin_comment, job.join_url, None)
        job.transaction_id = replacement
        job.revision += 1
        job.creation_in_flight = False
        job.event_id = job.join_url = None
        job.last_error = None
        job.attempts = 0
        job.available_at = now
        job.lease_token = job.lease_expires_at = None
        job.reconcile_at = None
    return JSONResponse({'status': 'queued'}, headers=microsoft_oauth_routes.HEADERS)



class PatchReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int
    acknowledge_checked: Literal[True]
    acknowledge_late_risk: Literal[True]

    @field_validator('expected_revision', mode='before')
    @classmethod
    def strict_revision(cls, value):
        if type(value) is not int:
            raise ValueError('revision must be an integer')
        return value

    @field_validator('acknowledge_checked', 'acknowledge_late_risk', mode='before')
    @classmethod
    def strict_confirmation(cls, value):
        if value is not True:
            raise ValueError('confirmation required')
        return value


@router.post('/admin/classes/{booking_id}/teams/patch-review')
def patch_review(booking_id: int, body: PatchReviewBody, admin=Depends(admin_user),
                 factory=Depends(microsoft_oauth_routes.get_session_factory)):
    try:
        with factory.begin() as db:
            booking = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
            generation = db.scalar(select(MicrosoftOAuthGeneration).where(MicrosoftOAuthGeneration.id == 1).with_for_update())
            credential = db.scalar(select(MicrosoftCredential).where(MicrosoftCredential.id == 1).with_for_update())
            job = db.scalar(select(TeamsSyncJob).where(TeamsSyncJob.booking_id == booking_id).with_for_update())
            if not booking or not job:
                raise HTTPException(404, 'Clase no encontrada')
            now = datetime.now(timezone.utc)
            if ((booking.status, job.desired) not in (('confirmed', 'upsert'), ('cancelled', 'delete'))
                    or job.revision != body.expected_revision
                    or (job.lease_token and not job.lease_expires_at)
                    or (job.lease_expires_at and _utc(job.lease_expires_at) > now)
                    or job.creation_in_flight
                    or job.last_error in ('creation_result_unknown', 'event_missing', 'recreation_account_mismatch')
                    or (job.desired == 'upsert' and (not job.event_id or not job.starts_at or not job.ends_at))):
                raise HTTPException(409, 'La clase requiere revisión adicional')
            if (not generation or not credential or credential.needs_reconnect
                    or not generation.tenant_id or not generation.account_object_id
                    or credential.account_object_id != generation.account_object_id):
                raise HTTPException(409, 'La cuenta Microsoft no está conectada')
            try:
                UUID(generation.tenant_id)
                UUID(generation.account_object_id)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(409, 'La identidad Microsoft no es verificable') from None
            operations = list(db.scalars(select(TeamsRemoteOperation).where(
                TeamsRemoteOperation.job_id == job.id,
                TeamsRemoteOperation.operation == 'patch',
                or_(TeamsRemoteOperation.state == 'in_flight',
                    and_(TeamsRemoteOperation.state == 'unknown', TeamsRemoteOperation.reviewed_at.is_(None)))
            ).order_by(TeamsRemoteOperation.id).with_for_update()))
            if (not operations or any(op.state != 'unknown' or not op.oauth_generation
                    or not op.event_id or (job.event_id and op.event_id != job.event_id)
                    or (op.tenant_id, op.account_object_id) != (generation.tenant_id, generation.account_object_id)
                    for op in operations)):
                raise HTTPException(409, 'Las operaciones requieren revisión adicional')
            if db.scalar(select(TeamsCreationAttempt.id).where(
                    TeamsCreationAttempt.job_id == job.id,
                    TeamsCreationAttempt.state.in_(('unknown', 'in_flight'))).limit(1)):
                raise HTTPException(409, 'Existe un intento de creación sin resolver')
            for op in operations:
                if op.reviewed_at is not None:
                    raise HTTPException(409, 'La operación ya fue revisada')
                op.reviewed_at = now
                op.reviewed_by_admin_id = admin.id
                op.reviewed_revision = job.revision
                op.reviewed_desired = job.desired
            db.flush()
    except IntegrityError:
        raise HTTPException(409, 'La operación ya fue revisada') from None
    return JSONResponse({'status': 'reviewed'}, headers=microsoft_oauth_routes.HEADERS)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
