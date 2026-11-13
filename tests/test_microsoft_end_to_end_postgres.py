"""Run the same full-stack scenarios in disposable PostgreSQL containers."""
import pytest
from ephemeral_postgres import postgres_test_db

from test_microsoft_end_to_end import (
    scenario, test_guest_booking_reconcile_retry_edit_cancel,
    test_registered_student_sees_reconciled_link,
)


@pytest.fixture
def engine(postgres_test_db):
    return postgres_test_db.engine
