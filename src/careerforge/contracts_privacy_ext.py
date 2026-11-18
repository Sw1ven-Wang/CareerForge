from datetime import datetime
from typing import Literal

from pydantic import Field

from .contracts import Contract


class PrivacyProposalInput(Contract):
    scope: Literal["all_personal_content"] = "all_personal_content"


class PrivacyConfirmInput(Contract):
    confirmation_nonce: str = Field(min_length=24, max_length=200)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledge_irreversible: Literal[True]


class RetentionHoldInput(Contract):
    principal_id: str
    category: Literal["personal_content"] = "personal_content"
    reason_code: Literal["legal_hold", "security_incident", "accounting_dispute"]
    expires_at: datetime
