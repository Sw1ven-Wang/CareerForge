from __future__ import annotations

import time
from copy import deepcopy
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from ..config import Settings
from ..contracts import ModelOutput, ToolCall
from ..errors import DomainError, LeaseLost, PauseBoundary
from ..lease_heartbeat import LeaseHeartbeat
from ..models import AgentRun, FundingAgreement, ModelReceipt, Namespace, ObjectRow, StageArtifact
from ..services.dependencies import assert_source_scope, check_closure
from ..services.evolution_feedback import feedback_source_dependencies, model_feedback_witnesses
from ..services.policy_inputs import run_policy_inputs
from ..services.privacy import assert_personal_access
from ..services.run_pause import retract_undispatched_model
from ..services.runtime import (
    Runtime,
    active_funding,
    event,
    is_public_system_run,
    leased,
    lock_run,
)
from ..util import canonical, fingerprint, uid
from .request_contract import (
    CONTEXT_ONCE_CONTRACT,
    LEGACY_REQUEST_CONTRACT,
    MODEL_REQUEST_MAX_BYTES,
    check_bytes,
    contract_error,
    pack_request,
    validate_request,
)


class ModelProvider(Protocol):
    profile_version: str
    synthetic: bool

    async def complete(self, request: dict[str, Any]) -> ModelOutput: ...


class OpenAICompatibleProvider:
    """Single-shot paid requests. No implicit SDK retries or automatic provider fallback."""

    synthetic = False

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        role_models: dict[str, str] | None = None,
    ):
        self.request_contract = settings.model_request_contract
        # Do not let a shared mutable role mapping change a frozen new-run profile.
        self.settings = (
            deepcopy(settings) if self.request_contract == CONTEXT_ONCE_CONTRACT else settings
        )
        self.client = client
        selected = settings.role_models if role_models is None else role_models
        self.role_models = (
            deepcopy(selected) if self.request_contract == CONTEXT_ONCE_CONTRACT else selected
        )
        self.profile_version = self._configuration_profile()

    def _configuration_profile(self) -> str:
        s = self.settings
        profile = fingerprint(
            {
                "base_url": s.model_base_url,
                "model": s.model_name,
                "roles": self.role_models,
                "structured_output": s.structured_output,
                "max_output": s.max_output_tokens,
                "embedding_model": s.embedding_model,
                "embedding_dimensions": s.embedding_dimensions,
                "rerank_model": s.rerank_model,
                "embedding_base_url": s.embedding_base_url,
                "rerank_url": s.rerank_url,
            }
        )
        if s.model_streaming:
            profile = fingerprint({"base_profile": profile, "transport": "chat-sse-complete-v1"})
        if self.request_contract == CONTEXT_ONCE_CONTRACT:
            return fingerprint({"base_profile": profile, "request_contract": self.request_contract})
        if self.request_contract != LEGACY_REQUEST_CONTRACT:
            raise contract_error()
        return profile

    def validate_config(self, role: str) -> None:
        s = self.settings
        if role == "embedding":
            valid = bool(s.embedding_base_url and s.embedding_api_key and s.embedding_model)
            endpoint = s.embedding_base_url
        elif role == "reranker":
            valid = bool(s.rerank_url and s.rerank_api_key and s.rerank_model)
            endpoint = s.rerank_url
        else:
            valid = bool(
                s.model_base_url and s.model_api_key and self.role_models.get(role, s.model_name)
            )
            endpoint = s.model_base_url
        try:
            parsed = httpx.URL(endpoint)
            valid = valid and parsed.scheme in {"http", "https"} and bool(parsed.host)
        except httpx.InvalidURL:
            valid = False
        if not valid:
            raise DomainError(
                "MODEL_NOT_CONFIGURED", "该角色的真实模型尚未配置；没有发送请求。", 503
            )

    async def complete(self, request: dict[str, Any]) -> ModelOutput:
        s = self.settings
        if self.request_contract == CONTEXT_ONCE_CONTRACT:
            self.validate_request(request)
        elif "request_contract" in request:
            raise contract_error()
        self.validate_config(request["role"])
        if request["role"] in {"embedding", "reranker"}:
            return await self._retrieval_call(request)
        if (
            not s.model_base_url
            or not s.model_api_key
            or not self.role_models.get(request["role"], s.model_name)
        ):
            raise DomainError("MODEL_NOT_CONFIGURED", "尚未配置真实模型，未自动使用模拟结果。", 503)
        body = self._chat_body(request)
        wire = (
            self.request_wire_bytes(request)
            if self.request_contract == CONTEXT_ONCE_CONTRACT
            else None
        )

        async def send(client: httpx.AsyncClient) -> ModelOutput:
            try:
                headers = {
                    "Authorization": "Bearer " + s.model_api_key,
                    **({"Content-Type": "application/json"} if wire is not None else {}),
                }
                payload = {"content": wire} if wire is not None else {"json": body}
                url = s.model_base_url.rstrip("/") + "/chat/completions"

                def check_status(response):
                    if response.status_code != 200:
                        error = DomainError(
                            "MODEL_HTTP_ERROR",
                            f"模型请求失败（HTTP {response.status_code}），未自动重试。",
                            502,
                        )
                        error.provider_http_status = response.status_code
                        raise error

                if s.model_streaming:
                    import asyncio

                    from .chat_stream import collect_chat_stream

                    async with asyncio.timeout(s.model_timeout):
                        async with client.stream(
                            "POST", url, headers=headers, **payload, timeout=s.model_timeout
                        ) as response:
                            check_status(response)
                            if (
                                "text/event-stream"
                                not in response.headers.get("content-type", "").lower()
                            ):
                                raise DomainError(
                                    "MODEL_RESULT_UNCERTAIN",
                                    "中转站未返回约定的模型流，未自动重试。",
                                    502,
                                )
                            raw = await collect_chat_stream(response)
                else:
                    response = await client.post(
                        url, headers=headers, **payload, timeout=s.model_timeout
                    )
                    check_status(response)
                    raw = response.json()
                choice = raw["choices"][0]
                message = choice["message"]
                usage = raw.get("usage") or {}
                # Store numeric metering only, not arbitrary provider diagnostic strings/secrets.
                numeric = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
                text = message.get("content") or ""
                import json

                structured = json.loads(text) if request.get("schema") else None
                calls = [
                    ToolCall(
                        id=c["id"],
                        name=c["function"]["name"],
                        arguments=json.loads(c["function"]["arguments"]),
                    )
                    for c in message.get("tool_calls") or []
                ]
                return ModelOutput(
                    text=text,
                    structured=structured,
                    tool_calls=calls,
                    finish_reason=choice.get("finish_reason", "unknown"),
                    usage=numeric,
                    provider_response_id=raw.get("id"),
                )
            except DomainError:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DomainError(
                    "MODEL_CONNECT_ERROR",
                    "无法建立模型连接；连接阶段重试已结束，未取得模型结果。",
                    502,
                ) from exc
            except (
                httpx.HTTPError,
                TimeoutError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
                raise DomainError(
                    "MODEL_RESULT_UNCERTAIN", "未取得有效的完整模型结果，停止重复付费调用。", 502
                ) from exc

        if self.client:
            return await send(self.client)
        # HTTPX retries only connection establishment (ConnectError/ConnectTimeout),
        # never a written request, HTTP response error or interrupted response stream.
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            return await send(client)

    def _chat_body(self, request: dict[str, Any]) -> dict[str, Any]:
        s = self.settings
        body = {
            "model": self.role_models.get(request["role"], s.model_name),
            "messages": request["messages"],
            "max_completion_tokens": request["max_output_tokens"],
        }
        if s.model_streaming:
            body.update(stream=True, stream_options={"include_usage": True})
        if request.get("tools"):
            body["tools"] = request["tools"]
        if request.get("schema"):
            # Some policy payloads contain open dictionaries. Do not silently send an
            # unsupported strict schema; choose the format BEFORE the paid attempt.
            mode = s.structured_output

            def closed(node):
                if isinstance(node, dict):
                    if (
                        node.get("type") == "object"
                        and node.get("additionalProperties") is not False
                    ):
                        return False
                    return all(closed(v) for v in node.values())
                if isinstance(node, list):
                    return all(closed(v) for v in node)
                return True

            strict = mode == "json_schema" or (mode == "auto" and closed(request["schema"]))
            body["response_format"] = (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "careerforge_output",
                        "strict": True,
                        "schema": request["schema"],
                    },
                }
                if strict
                else {"type": "json_object"}
            )
            if not strict:
                body["messages"] = [
                    *body["messages"],
                    {
                        "role": "user",
                        "content": "Return JSON only, validated against this schema: "
                        + canonical(request["schema"]),
                    },
                ]

        return body

    def request_wire_bytes(self, request: dict[str, Any]) -> bytes:
        """Exact HTTP JSON bytes, including schema fallback and provider wrappers."""
        if request["role"] in {"embedding", "reranker"}:
            context, s = request["context"], self.settings
            body = (
                {
                    "model": s.embedding_model,
                    "input": context["texts"],
                    "dimensions": s.embedding_dimensions,
                }
                if request["role"] == "embedding"
                else {
                    "model": s.rerank_model,
                    "query": context["query"],
                    "documents": context["documents"],
                }
            )
        else:
            body = self._chat_body(request)
        try:
            wire = httpx.Request("POST", "https://wire-budget.invalid/", json=body).content
        except (TypeError, ValueError, UnicodeError) as exc:
            raise contract_error() from exc
        check_bytes(wire)
        return wire

    def validate_request(self, request: dict[str, Any]) -> None:
        if (
            self.request_contract != CONTEXT_ONCE_CONTRACT
            or self.settings.model_request_contract != CONTEXT_ONCE_CONTRACT
            or self._configuration_profile() != self.profile_version
            or request.get("profile_version") != self.profile_version
            or request.get("max_output_tokens") != self.settings.max_output_tokens
        ):
            raise contract_error()
        validate_request(request)
        self.request_wire_bytes(request)

    async def _retrieval_call(self, request: dict[str, Any]) -> ModelOutput:
        s, context = self.settings, request["context"]
        embedding = request["role"] == "embedding"
        url = s.embedding_base_url.rstrip("/") + "/embeddings" if embedding else s.rerank_url
        key = s.embedding_api_key if embedding else s.rerank_api_key
        model = s.embedding_model if embedding else s.rerank_model
        if not model or not key or not url.startswith(("http://", "https://")):
            raise DomainError("RETRIEVAL_MODEL_NOT_CONFIGURED", "未配置对应的检索模型。", 503)
        body = (
            {"model": model, "input": context["texts"], "dimensions": s.embedding_dimensions}
            if embedding
            else {"model": model, "query": context["query"], "documents": context["documents"]}
        )

        wire = (
            self.request_wire_bytes(request)
            if self.request_contract == CONTEXT_ONCE_CONTRACT
            else None
        )

        async def send(client: httpx.AsyncClient) -> ModelOutput:
            try:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + key,
                        **({"Content-Type": "application/json"} if wire is not None else {}),
                    },
                    **({"content": wire} if wire is not None else {"json": body}),
                    timeout=s.model_timeout,
                )
                if response.status_code != 200:
                    raise DomainError("RETRIEVAL_MODEL_HTTP", "检索模型请求失败，未自动重试。", 502)
                data = response.json()
                if embedding:
                    items = data["data"]
                    # Sorting alone accepts duplicate/missing indices and can
                    # silently attach an embedding to the wrong input text.
                    indices = [item["index"] for item in items]
                    if any(type(index) is not int for index in indices) or sorted(indices) != list(
                        range(len(context["texts"]))
                    ):
                        raise ValueError("invalid embedding indices")
                    ordered = sorted(items, key=lambda item: item["index"])
                    result = {"vectors": [item["embedding"] for item in ordered], "model": model}
                else:
                    result = {
                        "scores": [
                            {"index": item["index"], "score": item["relevance_score"]}
                            for item in data["results"]
                        ]
                    }
                return ModelOutput(
                    structured=result,
                    usage={
                        k: v
                        for k, v in (data.get("usage") or {}).items()
                        if type(v) is int and v >= 0
                    },
                    finish_reason="stop",
                )
            except DomainError:
                raise
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                AttributeError,
            ) as exc:
                raise DomainError(
                    "MODEL_RESULT_UNCERTAIN", "未取得有效的完整模型结果，停止重复付费调用。", 502
                ) from exc

        if self.client:
            return await send(self.client)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await send(client)


def strict_schema(value: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-compatible strict object schemas: optional values are nullable, not absent."""
    from copy import deepcopy

    result = deepcopy(value)

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(result)
    return result


class ModelGateway:
    def __init__(self, runtime: Runtime, provider: ModelProvider, settings: Settings):
        self.runtime, self.provider, self.settings = runtime, provider, settings
        self.provider_validator = getattr(provider, "validate_request", None)
        self.request_contract = settings.model_request_contract
        if (
            self.request_contract not in {LEGACY_REQUEST_CONTRACT, CONTEXT_ONCE_CONTRACT}
            or getattr(provider, "request_contract", LEGACY_REQUEST_CONTRACT)
            != self.request_contract
            or (
                self.request_contract == CONTEXT_ONCE_CONTRACT
                and not callable(self.provider_validator)
            )
        ):
            raise contract_error()

    def request_payload(
        self,
        role: str,
        system: str,
        context: dict[str, Any],
        output: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        data_classes: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        access_dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        declared_data_classes = sorted(set(data_classes or ["public_source"]))
        dependency_ids = sorted(set(access_dependencies or []))
        request = {
            "role": role,
            "context": context,
            "messages": messages
            or [
                {"role": "system", "content": system},
                {"role": "user", "content": "UNTRUSTED_TASK_DATA\n" + canonical(context)},
            ],
            "schema": strict_schema(
                output_schema if output_schema is not None else output.model_json_schema()
            )
            if output
            else None,
            "tools": tools or [],
            "max_output_tokens": self.settings.max_output_tokens,
            "profile_version": self.provider.profile_version,
            "data_classes": declared_data_classes,
            "access_dependencies": dependency_ids,
        }
        return (
            pack_request(request, generated=not messages)
            if self.request_contract == CONTEXT_ONCE_CONTRACT
            else request
        )

    async def call(
        self,
        run_id: str,
        epoch: int,
        worker: str,
        key: str,
        role: str,
        system: str,
        context: dict[str, Any],
        output: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        data_classes: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        access_dependencies: list[str] | None = None,
        replay_only: bool = False,
    ) -> ModelOutput:
        with self.runtime.maintain_lease(run_id, epoch, worker) as heartbeat:
            return await self._call(
                run_id,
                epoch,
                worker,
                key,
                role,
                system,
                context,
                output,
                tools,
                data_classes,
                messages,
                output_schema,
                access_dependencies,
                replay_only,
                heartbeat=heartbeat,
            )

    async def _call(
        self,
        run_id: str,
        epoch: int,
        worker: str,
        key: str,
        role: str,
        system: str,
        context: dict[str, Any],
        output: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        data_classes: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        access_dependencies: list[str] | None = None,
        replay_only: bool = False,
        *,
        heartbeat: LeaseHeartbeat,
    ) -> ModelOutput:
        request = self.request_payload(
            role,
            system,
            context,
            output,
            tools,
            data_classes,
            messages,
            output_schema,
            access_dependencies,
        )
        declared_data_classes = request["data_classes"]
        dependency_ids = request["access_dependencies"]
        if len(canonical(request).encode()) > MODEL_REQUEST_MAX_BYTES:
            raise DomainError("MODEL_CONTEXT_LIMIT", "模型上下文超过合同限制。", 413)
        if self.request_contract == CONTEXT_ONCE_CONTRACT:
            validate_request(request)
        request_hash = fingerprint(request)
        with self.runtime.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            if (
                self.request_contract == CONTEXT_ONCE_CONTRACT
                and run.input_manifest.get("model_profile") != self.provider.profile_version
            ):
                raise DomainError("MODEL_PROFILE_CHANGED", "封装合同需要新建并固定模型配置的任务。")
            feedback_witnesses, policy_classes = self._input_witnesses(db, run, role)
            if not set(policy_classes) <= set(declared_data_classes):
                raise DomainError(
                    "EGRESS_OR_ROLE_DENIED", "继承策略的学习状态或来源正文缺少明确外发授权。", 403
                )
            if not set(feedback_source_dependencies(db, feedback_witnesses)) <= set(dependency_ids):
                raise DomainError(
                    "MODEL_DEPENDENCIES_MISSING", "模型调用缺少实际策略经验的来源依赖。", 403
                )
            funding = active_funding(db, run)
            funding_identifier = funding.id
            assert_source_scope(db, dependency_ids, funding.allowed_source_ids)
            request["funding_source_scope"] = sorted(funding.allowed_source_ids)
            privacy_generations: dict[str, int] = {}
            # Trusted callers pass their exact evidence references. Do not infer
            # authorization from model text or arbitrary identifiers inside a prompt.
            dependency_closure = check_closure(db, run.principal_id, dependency_ids)
            if (
                dependency_closure.private_source_content
                and "private_source" not in declared_data_classes
            ):
                raise DomainError(
                    "EGRESS_OR_ROLE_DENIED",
                    "私人知识或分类正文需要明确的private_source外发授权。",
                    403,
                )
            dependency_hash = dependency_closure.closure_hash
            request["access_closure_hash"] = dependency_hash
            if run.input_manifest.get("evolution_graph"):
                from ..services.evolution_privacy import validate_run_generations_in

                validate_run_generations_in(db, run, dependencies=dependency_ids)
            dependency_actor = run.principal_id
            owners = set(
                db.scalars(
                    select(Namespace.owner_id)
                    .join(ObjectRow, ObjectRow.namespace_id == Namespace.id)
                    .where(ObjectRow.id.in_(dependency_ids), Namespace.kind != "shared_public")
                )
            )
            public_system = is_public_system_run(db, run)
            if public_system and (owners or not dependency_closure.public_only):
                raise DomainError("EGRESS_OR_ROLE_DENIED", "纯公共系统任务不得外发私人依赖。", 403)
            if not public_system:
                namespace = db.get(Namespace, run.namespace_id)
                if namespace is None:
                    raise DomainError("NOT_FOUND", "任务知识域不存在。", 404)
                owners.add(run.principal_id)
                if namespace.kind != "shared_public":
                    owners.add(namespace.owner_id)
            privacy_generations = {
                owner: assert_personal_access(db, owner) for owner in sorted(owners)
            }
            request["privacy_generations"] = privacy_generations
            if self.request_contract == CONTEXT_ONCE_CONTRACT:
                validate_request(request, authority=True)
                assert self.provider_validator is not None
                self.provider_validator(request)
            request_hash = fingerprint(request)
            if role not in funding.allowed_roles or not set(declared_data_classes) <= set(
                funding.allowed_data_classes
            ):
                raise DomainError("EGRESS_OR_ROLE_DENIED", "未授权的模型角色或数据外发。", 403)
            previous = db.scalar(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run_id, ModelReceipt.stage_key == key
                )
            )
            if previous:
                if previous.request_hash != request_hash:
                    raise DomainError(
                        "MODEL_REPLAY_CHANGED", "请求内容或模型版本已改变，禁止静默重放。"
                    )
                if previous.status != "not_dispatched":
                    if previous.status != "completed" or previous.response is None:
                        raise DomainError(
                            "MODEL_RESULT_UNCERTAIN", "之前的请求结果未知，不自动再次收费。"
                        )
                    return self.validate_result(
                        ModelOutput.model_validate(previous.response), output
                    )
            # Internal execution restriction, deliberately absent from the wire
            # request/hash. Historical unsafe contexts may reuse a genuine exact
            # completed receipt, but never reserve or dispatch another request.
            # Keeping this decision in the reservation transaction also closes
            # the service-preflight/receipt-removal race.
            if replay_only:
                raise DomainError(
                    "MODEL_REPLAY_ONLY",
                    "历史上下文缺少完整外发依赖，只能读取已有完整收据；请显式新建任务。",
                    409,
                )
            # A subclass's arm limit is checked while the same leased run lock
            # protects the actual reservation, never in a separate transaction.
            self._check_attempt_budget(db, run, key)
            check = getattr(self.provider, "validate_config", None)
            if check is not None:
                check(role)
            if run.calls_consumed + run.held_calls >= run.call_cap:
                raise DomainError("MODEL_BUDGET", "本任务模型额度已用尽。")
            if run.parent_id:
                parent = db.get(AgentRun, run.parent_id)
                if parent is None:
                    raise LeaseLost()
                if parent.held_calls <= 0:
                    raise DomainError("MODEL_BUDGET", "子任务没有预留额度。")
                parent.held_calls -= 1
                parent.calls_consumed += 1
            run.calls_consumed += 1
            funding.calls_held -= 1
            funding.calls_consumed += 1
            if previous is not None:
                receipt = previous
                receipt.status, receipt.epoch = "attempted", epoch
            else:
                receipt = ModelReceipt(
                    id=uid(),
                    run_id=run.id,
                    stage_key=key,
                    payer_id=funding.payer_id,
                    epoch=epoch,
                    role=role,
                    request_hash=request_hash,
                    profile_version=self.provider.profile_version,
                )
                db.add(receipt)
                db.add(
                    StageArtifact(
                        run_id=run.id,
                        stage_key="model-access:" + receipt.id,
                        input_hash=run.input_hash,
                        payload={
                            "kind": "model_access_witness",
                            "receipt_id": receipt.id,
                            "request_hash": request_hash,
                            "access_dependencies": dependency_ids,
                            "closure_hash": dependency_hash,
                            "feedback_witnesses": feedback_witnesses,
                        },
                    )
                )
            event(
                db,
                run,
                "model.attempted",
                {"role": role, "receipt_id": receipt.id, "request_hash": request_hash},
            )
            receipt_id = receipt.id

        # All network I/O occurs after committing the request reservation.
        from ..observability import MODEL_CALLS, MODEL_LATENCY, tracer

        started = time.monotonic()
        span = tracer.start_span(
            "model.request",
            attributes={"agent.role": role, "model.profile": self.provider.profile_version},
        )
        outcome = "error"
        dispatched = False
        try:
            # This is the last authorization boundary before dispatch. A request already
            # authorized/in flight cannot be recalled from an external provider; a later
            # privacy generation fence prevents its body from ever re-entering storage.
            heartbeat.check()
            self.runtime.validate(run_id, epoch, worker)
            with self.runtime.db.read() as db:
                current_funding = db.scalar(
                    select(FundingAgreement)
                    .where(FundingAgreement.id == funding_identifier)
                    .with_for_update(read=True)
                )
                if current_funding is None:
                    raise DomainError("FUNDING_UNAVAILABLE", "费用授权记录不可用。")
                self._check_funding_scope(current_funding, role, declared_data_classes)
                current_run = db.get(AgentRun, run_id, populate_existing=True)
                if current_run is None or self._input_witnesses(db, current_run, role) != (
                    feedback_witnesses,
                    policy_classes,
                ):
                    raise DomainError(
                        "EXPERIENCE_CHANGED", "模型输入的优化授权或经验已经改变。", 409
                    )
                assert_source_scope(db, dependency_ids, current_funding.allowed_source_ids)
                if (
                    check_closure(db, dependency_actor, dependency_ids).closure_hash
                    != dependency_hash
                ):
                    raise DomainError(
                        "MODEL_DEPENDENCIES_CHANGED", "模型输入依赖已变化，停止外发。"
                    )
                if current_run.input_manifest.get("evolution_graph"):
                    from ..services.evolution_privacy import validate_run_generations_in

                    validate_run_generations_in(db, current_run, dependencies=dependency_ids)
                for owner, generation in privacy_generations.items():
                    assert_personal_access(db, owner, expected_generation=generation)
            # Source/privacy authorization can synchronously block after the
            # earlier lease check. Recheck current ownership/deadline immediately
            # before dispatch; the keeper never substitutes for this fence.
            heartbeat.check()
            self.runtime.validate(run_id, epoch, worker)
            # A keeper may fail while synchronous authorization is in progress.
            # Do not dispatch a new request after observing that failure.
            heartbeat.check()
            if self.request_contract == CONTEXT_ONCE_CONTRACT:
                validate_request(request, authority=True)
                assert self.provider_validator is not None
                self.provider_validator(request)
            dispatched = True
            result = await self.provider.complete(request)
            privacy_error: DomainError | None = None
            with self.runtime.db.transaction() as db:
                saved = db.get(ModelReceipt, receipt_id)
                if saved is None:
                    raise DomainError(
                        "MODEL_RECEIPT_MISSING", "模型调用收据缺失，停止后续操作。", 500
                    )
                try:
                    # Preserve an issued receipt even after lease loss/cancellation, but
                    # never persist a body outside the CURRENT funded source scope.
                    current_funding = db.scalar(
                        select(FundingAgreement)
                        .where(FundingAgreement.id == funding_identifier)
                        .with_for_update(read=True)
                    )
                    if current_funding is None:
                        raise DomainError("FUNDING_UNAVAILABLE", "费用授权记录不可用。")
                    self._check_funding_scope(current_funding, role, declared_data_classes)
                    current_run = db.get(AgentRun, run_id, populate_existing=True)
                    if current_run is None or self._input_witnesses(db, current_run, role) != (
                        feedback_witnesses,
                        policy_classes,
                    ):
                        raise DomainError(
                            "EXPERIENCE_CHANGED", "模型返回时优化授权或经验已经改变。", 409
                        )
                    assert_source_scope(db, dependency_ids, current_funding.allowed_source_ids)
                    if (
                        check_closure(db, dependency_actor, dependency_ids).closure_hash
                        != dependency_hash
                    ):
                        raise DomainError(
                            "MODEL_DEPENDENCIES_CHANGED", "模型输入依赖已变化，丢弃返回正文。"
                        )
                    if current_run.input_manifest.get("evolution_graph"):
                        from ..services.evolution_privacy import validate_run_generations_in

                        validate_run_generations_in(db, current_run, dependencies=dependency_ids)
                    for owner, generation in privacy_generations.items():
                        assert_personal_access(db, owner, expected_generation=generation)
                    if saved.status == "redacted":
                        raise DomainError(
                            "MODEL_RESULT_REDACTED", "已移除的模型正文不能恢复。", 409
                        )
                except DomainError as exc:
                    privacy_error = exc
                    saved.status, saved.response = "redacted", None
                    saved.usage = {
                        k: v for k, v in result.usage.items() if type(v) is int and v >= 0
                    }
                else:
                    saved.status = "completed"
                    saved.response = result.model_dump(mode="json")
                    saved.usage = result.usage
            if privacy_error is not None:
                raise privacy_error
            heartbeat.check()
            self.runtime.validate(run_id, epoch, worker)
            checked = self.validate_result(result, output)
            outcome = "completed"
            return checked
        except PauseBoundary:
            if not dispatched:
                with self.runtime.db.transaction() as db:
                    locked_run = lock_run(db, run_id)
                    if locked_run is not None:
                        retract_undispatched_model(db, locked_run, receipt_id, epoch)
            raise
        except BaseException as exc:
            with self.runtime.db.transaction() as db:
                saved = db.get(ModelReceipt, receipt_id)
                if saved is not None and saved.status not in {"completed", "redacted"}:
                    saved.status = "unknown"
                    http_status = getattr(exc, "provider_http_status", None)
                    if type(http_status) is int and 100 <= http_status <= 599:
                        locked_run = lock_run(db, run_id)
                        if locked_run is not None:
                            event(
                                db,
                                locked_run,
                                "model.http_error",
                                {
                                    "receipt_id": receipt_id,
                                    "role": role,
                                    "http_status": http_status,
                                },
                            )
            raise
        finally:
            MODEL_CALLS.labels(role, outcome).inc()
            MODEL_LATENCY.labels(role).observe(time.monotonic() - started)
            span.set_attribute("outcome", outcome)
            span.end()

    @staticmethod
    def _input_witnesses(db, run: AgentRun, role: str):
        from ..services.ordinary_checker import validate_ordinary_run_in
        from ..services.session_tutor import validate_session_run_in

        validate_session_run_in(db, run)
        validate_ordinary_run_in(db, run)
        policy = run_policy_inputs(db, run, role)
        combined = {
            item["experience_id"]: item
            for item in [*model_feedback_witnesses(db, run), *policy.witnesses]
        }
        return [combined[key] for key in sorted(combined)], policy.data_classes

    def policy_egress_inputs(
        self,
        run_id: str,
        role: str,
        data_classes: list[str] | None = None,
        access_dependencies: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Trusted caller preparation; call() independently rechecks every requirement.

        New explicit run contracts can include evolved-policy lineage. Historical
        contracts are never silently upgraded, and no-candidate requests are byte
        identical. This method does not reserve calls or grant funding/permissions.
        """
        classes = set(data_classes or ["public_source"])
        dependencies = set(access_dependencies or [])
        with self.runtime.db.read() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise DomainError("NOT_FOUND", "模型执行任务不存在。", 404)
            policy = run_policy_inputs(db, run, role)
            missing = (
                not set(policy.data_classes) <= classes
                or not set(policy.access_dependencies) <= dependencies
            )
            if missing and not policy.preparation_authorized:
                raise DomainError(
                    "POLICY_EGRESS_VERSION", "历史任务缺少继承策略的外发合同；须显式新建任务。", 409
                )
            classes.update(policy.data_classes)
            dependencies.update(policy.access_dependencies)
        return sorted(classes), sorted(dependencies)

    def remaining_calls_in(self, db, run: AgentRun) -> int:
        """Caller owns the leased run lock; this estimate grants no extra calls."""
        return max(0, run.call_cap - run.calls_consumed - run.held_calls)

    def _check_attempt_budget(self, db, run: AgentRun, key: str) -> None:
        """Optional stricter gateway budget; called inside atomic reservation."""
        if not run.input_manifest.get("execution_contract_version"):
            return
        from ..db import aware, clock
        from ..services.build_execution import (
            UNLIMITED_BUILD_EXECUTION,
            validate_execution_manifest,
        )

        plan = validate_execution_manifest(run.input_manifest, run.call_cap)
        assert plan is not None
        root = db.get(AgentRun, run.parent_id) if run.parent_id else run
        if root is None or root.calls_consumed >= plan["call_capacity"]:
            raise DomainError("BUILD_BUDGET", "冻结执行计划的调用额度已用尽。")
        required = plan["planning_call_seconds"] + plan["commit_reserve_seconds"]
        deadline = aware(root.deadline_at)
        if plan["version"] == UNLIMITED_BUILD_EXECUTION:
            # An authorization shortened after admission is never stretched to the old deadline.
            funding = db.get(FundingAgreement, root.funding_id)
            if funding is None:
                raise DomainError("FUNDING_UNAVAILABLE", "费用授权不存在。", 403)
            deadline = min(deadline, aware(funding.expires_at))
        if (deadline - clock(db)).total_seconds() < required:
            raise DomainError(
                "FUNDING_TIME_BUDGET"
                if plan["version"] == UNLIMITED_BUILD_EXECUTION
                else "BUILD_TIME_BUDGET",
                "剩余期限不足以容纳下一次调用的冻结规划假设和提交预留；未发起或计费新调用。"
                "既有任务不会延长，可用新的请求键调整 duration_minutes 或规划参数。",
            )

    @staticmethod
    def _check_funding_scope(funding: FundingAgreement, role: str, data_classes: list[str]) -> None:
        if role not in funding.allowed_roles or not set(data_classes) <= set(
            funding.allowed_data_classes
        ):
            raise DomainError("EGRESS_OR_ROLE_DENIED", "模型角色或数据外发授权已改变。", 403)

    def validate_result(self, result: ModelOutput, output: type[BaseModel] | None) -> ModelOutput:
        output_tokens = result.usage.get("completion_tokens", result.usage.get("output_tokens"))
        if output_tokens is not None and (type(output_tokens) is not int or output_tokens < 0):
            raise DomainError("PROVIDER_USAGE_INVALID", "供应商用量无效。", 422)
        if output_tokens is not None and output_tokens > self.settings.max_output_tokens:
            raise DomainError(
                "PROVIDER_OUTPUT_OVER_LIMIT", "供应商报告输出超限，停止后续调用。", 422
            )
        if result.finish_reason not in {"stop", "tool_calls"}:
            raise DomainError("MODEL_TRUNCATED", "模型返回未完成的内容。", 422)
        if output:
            if result.tool_calls or result.finish_reason != "stop":
                raise DomainError("MODEL_SCHEMA", "结构化响应中不允许额外工具请求。", 422)
            try:
                output.model_validate(result.structured)
            except ValidationError as exc:
                raise DomainError("MODEL_SCHEMA", "模型响应不符合严格数据合同。", 422) from exc
        return result
