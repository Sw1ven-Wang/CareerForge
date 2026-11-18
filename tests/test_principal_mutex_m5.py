"""Lock SQL shape only; real mutex/FK/race proofs run separately on PostgreSQL."""

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from careerforge.db import locked
from careerforge.models import FundingAgreement, LearningPlan, Principal, Turn
from careerforge.models_learning_ext import LearningProfile


class CaptureSession:
    def __init__(self):
        self.statements = []
        self.row = object()

    def scalar(self, statement):
        self.statements.append(statement)
        return self.row


def test_principal_mutex_is_no_key_update_not_key_share():
    session = CaptureSession()
    for _ in range(2):
        assert locked(session, Principal, "exact-owner") is session.row
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert str(compiled).endswith("FOR NO KEY UPDATE")
        assert compiled.params == {"id_1": "exact-owner"}
        assert statement.get_execution_options()["populate_existing"] is True
        assert statement._for_update_arg.read is False


@pytest.mark.parametrize("entity", [FundingAgreement, LearningPlan, LearningProfile, Turn])
def test_nonprincipal_authority_lock_strength_is_unchanged(entity):
    session = CaptureSession()
    locked(session, entity, "exact-id")
    statement = session.statements[0]
    assert str(statement.compile(dialect=postgresql.dialect())).endswith("FOR UPDATE")
    assert statement.get_execution_options()["populate_existing"] is True


def test_sqlite_keeps_its_existing_no_row_lock_clause_behavior():
    session = CaptureSession()
    locked(session, Principal, "owner")
    assert "FOR " not in str(session.statements[0].compile(dialect=sqlite.dialect()))
