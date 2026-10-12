"""External I/O is outside database stage transactions; one accounted call, no retry."""

import asyncio

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from careerforge.application.job_agent_service import (
    VERSION,
    request_for,
    save_facts,
    snapshot_for_run,
    validated_facts,
)
from careerforge.application.model_call_service import invoke_model
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    heartbeat,
    leased,
    transition,
)
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.infrastructure.llm.provider import ModelError


async def call(run_id, epoch, worker, request, base_url):
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实模型未启用或未配置。")
    if base_url != settings.openai_base_url:
        raise BusinessError(409, "PROVIDER_CHANGED", "模型服务地址已变化，请重新创建运行。")
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=settings.openai_api_key.get_secret_value(),
            client=client,
            timeout_seconds=settings.real_agent_timeout_seconds,
        )
        task = asyncio.create_task(
            invoke_model(run_id, epoch, worker, provider, request, max_retries=0)
        )
        factory = sessionmaker(get_engine(), expire_on_commit=False)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    with factory.begin() as db:
                        heartbeat(db, run_id, epoch, worker)
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def execute(run_id, worker):
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    try:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            snapshot = snapshot_for_run(db, run)
            if artifact(db, run, "result", VERSION):
                transition(db, run, "succeeded")
                return
            request = request_for(snapshot, run.input_manifest)
            base_url = run.input_manifest["base_url"]
            if artifact(db, run, "validate_inputs", VERSION) is None:
                commit_stage(
                    db,
                    run_id,
                    epoch,
                    worker,
                    "validate_inputs",
                    VERSION,
                    {
                        "job_snapshot_id": snapshot.job_snapshot_id,
                        "prompt_version": run.input_manifest["prompt_version"],
                        "model": request.model,
                        "reasoning_effort": "low",
                    },
                )
        result = asyncio.run(call(run_id, epoch, worker, request, base_url))
        if result is None:
            return
        if result.finish_reason != "stop":
            raise ModelError("invalid_response", retryable=False)
        facts, unresolved = validated_facts(snapshot, result)
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            snapshot_for_run(db, run)
            output = save_facts(db, run, facts, unresolved, result)
            leased(db, run_id, epoch, worker)
            commit_stage(db, run_id, epoch, worker, "result", VERSION, output)
            event(
                db,
                run,
                "tool.summary",
                {"candidates": len(facts), "unresolved": len(unresolved), "review_required": True},
            )
            transition(db, run, "succeeded" if facts else "partially_succeeded")
    except (BusinessError, ModelError, ValidationError, ValueError) as error:
        with factory.begin() as db:
            try:
                run = leased(db, run_id, epoch, worker)
            except BusinessError:
                return
            event(
                db,
                run,
                "stage.failed",
                {"error": getattr(error, "code", "invalid_candidate"), "automatic_retry": False},
            )
            transition(db, run, "failed")
