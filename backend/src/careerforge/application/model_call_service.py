from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from careerforge.infrastructure.db.models import ModelCallReceipt
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.provider import ModelProvider
from careerforge.schemas.model_io import ModelRequest, ModelResult


def settle_receipt(
    call_id: UUID,
    usage: dict[str, Any] | None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    """Allow late accounting after cancellation/lease loss, without business effects."""
    factory = sessionmaker(get_engine())
    with factory.begin() as db:
        receipt = db.scalar(
            select(ModelCallReceipt).where(ModelCallReceipt.id == call_id).with_for_update()
        )
        if receipt is None:
            raise ValueError("Unreserved provider call")
        if receipt.status == "completed":
            if receipt.usage != usage:
                raise ValueError("Receipt already settled differently")
            return
        receipt.status = "completed" if usage is not None else "unknown"
        receipt.usage = usage
        if diagnostics is not None:
            receipt.diagnostics = {**(receipt.diagnostics or {}), **diagnostics}


async def invoke_model(
    run_id: UUID,
    epoch: int,
    worker: str,
    provider: "ModelProvider",
    request: "ModelRequest",
    max_retries: int = 1,
) -> "ModelResult | None":
    """One bounded, accounted entry point for both FakeModel and real adapters."""
    import asyncio

    from careerforge.application.run_service import reserve_call
    from careerforge.infrastructure.llm.provider import ModelError

    if not 0 <= max_retries <= 1:
        raise ValueError("At most one transient retry is allowed")
    factory = sessionmaker(get_engine())
    for attempt in range(max_retries + 1):
        describe = getattr(provider, "request_diagnostics", None)
        diagnostics = describe(request) if describe is not None else None
        with factory.begin() as db:
            call_id = reserve_call(db, run_id, epoch, worker, request.model_dump(mode="json"))
            if call_id is not None and diagnostics is not None:
                receipt = db.get(ModelCallReceipt, call_id)
                if receipt is None:
                    raise ValueError("Unreserved provider call")
                receipt.diagnostics = diagnostics
        if call_id is None:
            return None
        try:
            result = await provider.complete(request)
        except ModelError as error:
            settle_receipt(
                call_id,
                error.usage.model_dump() if error.usage is not None else None,
                error.diagnostics,
            )
            if not error.retryable or attempt == max_retries:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))
            continue
        except BaseException:
            settle_receipt(call_id, None)
            raise
        settle_receipt(call_id, result.usage.model_dump(), result.diagnostics)
        return result
    raise AssertionError("Unreachable bounded retry")
