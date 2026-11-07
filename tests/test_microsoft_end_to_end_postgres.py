"""Run the same full-stack scenarios in a unique PostgreSQL schema."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from test_microsoft_end_to_end import (
    scenario, test_guest_booking_reconcile_retry_edit_cancel,
    test_registered_student_sees_reconciled_link,
)


@pytest.fixture
def engine():
    url = os.environ.get('MICROSOFT_E2E_POSTGRES_URL')
    if not url:
        pytest.skip('MICROSOFT_E2E_POSTGRES_URL must name a disposable PostgreSQL database')
    root = create_engine(url)
    assert root.dialect.name == 'postgresql'
    schema = 'microsoft_e2e_' + uuid4().hex
    with root.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        yield db
    finally:
        db.dispose()
        with root.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
