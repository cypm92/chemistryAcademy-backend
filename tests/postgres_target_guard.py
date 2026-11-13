"""Syntactic preflight only; never sufficient authorization for DDL.

The db hostname may resolve to an arbitrary server. A caller MUST additionally
attest the actual Compose container, network, peer, database and role before
any connection is used for writes. No database operations are performed here.
"""
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def validate_local_test_url(value):
    """Reject unapproved URL shapes without revealing connection secrets."""
    try:
        url = make_url(value)
        valid = (
            url.drivername in {'postgresql', 'postgresql+psycopg2'}
            and url.host == 'db'
            and url.port in (None, 5432)
            and url.database == 'beciencia_pytest_disposable'
            and url.username == 'beciencia'
            and not url.query
        )
        if valid:
            return url
    except (ArgumentError, TypeError, ValueError):
        pass
    raise ValueError('PostgreSQL test URL rejected') from None
