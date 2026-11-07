"""Synthetic identity and provenance for Teams worker tests; never real credentials."""
from uuid import uuid4

from app.microsoft_auth_models import MicrosoftOAuthGeneration
from app.microsoft_refresh import MicrosoftAccessTokenIdentity
from app.teams_models import TeamsCreationAttempt

IDENTITY = MicrosoftAccessTokenIdentity('fake-token', 'tenant', 'account', 4)


def seed_account(db):
    db.add(MicrosoftOAuthGeneration(id=1, generation=4, tenant_id='tenant', account_object_id='account'))


def seed_identified_event(db, job):
    db.flush()
    db.add(TeamsCreationAttempt(job_id=job.id, transaction_id=str(uuid4()), revision=job.revision,
        tenant_id='tenant', account_object_id='account', oauth_generation=4,
        state='identified', event_id=job.event_id, join_url=job.join_url))
