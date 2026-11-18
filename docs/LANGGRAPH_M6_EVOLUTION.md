# M6 Evolution graphs: API, authority, recovery and rollback

This is an engineering implementation candidate, not online/native acceptance. All automatic tests in this candidate use transparently synthetic public fixtures. Production promotion, real feedback acceptance, unseen independently qualified evaluation, and M7 remain unaccepted. No new default engine rollout is authorized.

## Explicit admission and ownership

`CAREERFORGE_EVOLUTION_GRAPH_ENABLED=1` explicitly enables **new** Evolution workflows. The default is off. Non-test execution requires a persistent PostgreSQL checkpointer. Global `workflow_engine=legacy` and the old default graph map remain unchanged. Disabling new admission does not reinterpret existing executions or prevent original idempotent receipt replay.

`POST /api/v1/conversations/{conversation_id}/evolution` accepts the required caller-only marker `contract: evolution-dialogue-v1`. An ordinary feedback message or coordinator action is not authority to spend, evaluate, promote, or roll back.

Example shape, with placeholder IDs replaced by real returned references:

```json
{
  "contract": "evolution-dialogue-v1",
  "idempotency_key": "explicit-candidate-1",
  "parent_workflow_id": "optional-existing-learning-workflow",
  "intent": {
    "type": "candidate",
    "target": "teaching_skill",
    "experience_ids": ["original-consented-experience"],
    "funding_id": "explicit-candidate-grant"
  }
}
```

The response is an immutable admission receipt, not a paid result. Fetch `GET /api/v1/conversations/{conversation_id}/evolution/{workflow_id}` for current validated references. The independent child does not take over learning focus, including on resume. A separate explicit focus command remains available.

Continuing commands include the exact `workflow_id` and `expected_workflow_revision`; they must not specify a new parent. Intents:

| Intent | Required authority and references |
|---|---|
| candidate | One of teaching_skill/routing_policy/retrieval_recipe/research_heuristic; original consented experience IDs; optional candidate funding |
| authorize_candidate | Exact pending request ID and request hash, explicit funding ID; no reauthorization of a consumed request |
| replay | Same session's original candidate ID plus existing ReplayInput (dataset ID, independent funding, per-arm caps/options and caller key) |
| promote | Same session's original candidate/evaluation IDs, exact head revision, `confirm: PROMOTE` |
| rollback | Exact target, head revision, from/to policy IDs, `confirm: ROLLBACK`; may start a separate command session |
| reject | Explicit reason; no policy publication |

Candidate without funding waits with zero paid Run. Candidate and replay each create a separate root AgentRun, reservation, deadline, lease, ModelReceipt set and WorkflowRunLink. They are not Runtime child allocations. A second key cannot consume a marked request again. Failed/inconclusive replay ends without promotion. A passing evaluation only waits for an explicit promotion command. Training consent, model-assessment candidates, real user feedback and paid authorization remain distinct.

## Actual graphs and reference-only state

Paid bindings: `(evolution, evolution-candidate-v1, 1)` and `(evolution_replay, evolution-replay-v1, 1)`. Unpaid owner: `(evolution_session, langgraph, evolution-session-v1, 1)`.

```text
candidate: validate -> invoke_reflection -> validate_patch -> commit_candidate -> finish
replay: validate -> select_arm -> execute_arm -> score_arm -> select_arm
                                      ... -> aggregate -> commit_evaluation -> finish
session: route -> dispatch/reconcile/effect -> route
          | authorization/run/replay/promotion/control interrupt
          -> completed
```

These nodes call extracted domain stages, never a whole legacy `execute`/`execute_replay` wrapper. Legacy entry points retain their original wire/schema/key behavior using shared domain helpers. Case order is counterbalanced, original arm call caps remain authoritative.

Paid checkpoint fields are Run ID, graph/schema versions, current stage/result key, and replay arm ordinal/name. Session state holds execution/workflow/binding/version references. Waits hold request ID/sequence, with command/generation for pause. No feedback body, prompt, answer, private execution, case label, policy content, evidence or spend authority enters checkpoints. DB state is authoritative; a saved cursor cannot skip missing business stages.

The replay recursion limit is derived from the exact frozen topology: `6 + 6 * case_count`, at most 306 for 50 cases. The original total call cap remains at most 100. This does **not** increase the global 64 default. A no-progress graph fails with `GRAPH_STEP_LIMIT`, preserves receipts and settles the original Run; it does not create another Run or retry a paid call. Fifty teaching cases at cap1 reserve 100 calls and execute 100 arms, but the original two-call Tutor preflight permits zero actual model calls. This is topology/budget capacity evidence, not 100 successful model calls.

## Private ledger and historical truth

Existing private StageArtifact keys:

- `reflection.validated.v1`: original receipt ID, exact reconstructed request hash and response hash.
- `replay:{case_ordinal}:{arm}:execution.v1`: versioned private execution with run/input/case/arm/policy binding, elapsed timing, actual outputs/receipt IDs/hashes and source dependencies.
- `replay:{case_ordinal}:{arm}:result`: independently reconstructed trace.
- `replay.aggregate.v1`: trace/report hashes only.
- `evolution-access:{receipt_id}`: original role, data classes, source/closure witness, bound to the gateway's `model-access:` artifact.
- `result`: exact candidate or evaluation result.

All readers verify the StageArtifact row's input_hash, not Runtime.stage truthiness. Privacy source erasure can reach copied outputs through explicit dependency references. Consent withdrawal redacts private evolution artifacts/receipts while preserving IDs, metering and immutable body-free effect facts. A separate durable withdrawal projection fences sessions even while paused without inverting Principal/conversation/feedback/funding locks. The centralized Principal lock remains PostgreSQL `FOR NO KEY UPDATE`.

Completed model receipts are replayed under original keys and exact hashes. Unknown/attempted results are never fabricated or retried under a new key. If a process dies after receipt completion but before the private execution artifact, original full latency cannot be recovered: the resumed measurement is marked incomplete, not zero-cost or fast. Once the execution artifact exists, its original timing survives score/aggregate recovery.

Leased reconciliation runs before Runtime.finish and requires a valid running lease and real committed result. Session/release terminal reconciliation additionally requires succeeded and budget_settled. New release gates validate private arms, report/results, receipt and current authority in the same domain transaction; historical promoted/rolled-back results do not run through active-candidate admission.

`GET /api/v1/evolution/runs/{run_id}/history` returns only an exact body-free schema from the atomic `evolution.paid.effect.v1` receipt. It distinguishes `historical_effect_committed` from `current_evidence_available` and always says `release_authorized: false`. Withdrawal may make evidence unavailable without erasing the truthful fact of an already committed effect. This API never dumps arbitrary receipt result keys.

Promotion and rollback use immutable `evolution.promote.v1` / `evolution.rollback.v1` command effect receipts committed atomically with policy/head/audit effects. Same-key replay returns the original effect, not a guessed current head. Current availability is separately recomputed. A new rollback command can legally walk another ancestor; duplicate delivery of the old command cannot.

## Migration and compatibility

`cf166_evolution_graph` follows verified M5 `cf166_practice_graph` and adds `workflow_evolution_requests`. Real FKs, per-execution sequence uniqueness, command/Run uniqueness, kind/status and paired authorization constraints are enforced. There is no historical manifest/hash backfill. Downgrade preflights M6 and all downstream retained-history conditions before any irreversible SQLite DDL. Old legacy rows alone do not block a valid roundtrip. Never delete history to force downgrade.

New identity covers extracted modules, domain/runtime/adapter/authorization/control modules and the explicit topology. The old `engine_versions()` and feedback-target creation hash algorithms remain unchanged. Their actual source hashes can honestly change during extraction; old evidence is not rewritten or called compatible merely because behavior looks similar. Keep the original verified deployment/environment for hash-bound pending legacy replay and old feedback-target contracts. New software may safely reject these bindings. Old result IDs/hash/metering may be read as history where the original contract allows, not repurposed for new release authority.

Application, worker, CLI and drain still share the original lease/outbox entry points. Analysis, Review, coding, retrieval indexing, ordinary/legacy chat, plans and legacy evolution retain their domain adapters; they are not all falsely reported as migrated graphs. `research` remains a historical Runtime allowlisted name without an independent Application handler. Any pending such Run is an explicit compatibility blocker, not permission to guess an executor. Deployment must inventory actual unfinished Run/session bindings and old strict-source contracts; no production database has been accessed by this engineering work.

## Safe rollback and remaining gates

1. Disable new M6 admission. Retain M6 handlers/checkpoints for existing frozen sessions and Runs, or cooperatively pause them. Never reinterpret them as legacy or rewrite their hashes.
2. Compare command receipts, model/external request states, stage/evaluation results, policy/head/audit facts and settlement before retrying. Unknown is not proof that no request was sent.
3. Preserve historical data and privacy tombstones. Keep original compatible deployments for strict-hash old work; do not delete legacy handlers based on name counts.
4. Policy rollback is a separate explicitly approved command with current CAS and exact valid ancestor, not a side effect of software feature rollback.
5. Final canonical+complete source manifests, same-hash regression, independent review, real PostgreSQL/Celery exit/recovery evidence and packaging checks must be collected before engineering acceptance. Actual native/online feedback and unseen qualified evaluation remain separately blocked under current capability/authorization limits. M7/default promotion and legacy removal remain prohibited.

## Measured synthetic engineering cost and safe deployment inventory

One pre-freeze scale attempt completed 12 cases/cap2 in 94.983 seconds (48 real fixture-provider calls), 50 cases/cap1 in 885.660 seconds (100 arm artifacts, zero replay model calls), and the no-progress bound test in 9.269 seconds. These are CPU/database/checkpoint costs on this test environment, not provider monetary costs or native quality. The 50-case first/last 25-arm mean cycles were 7.856/9.405 seconds. Repeated whole-publication validation and completed-prefix verification dominate compared with approximately 34–36ms Tutor preflight. A read-only diagnostic found 50 publication-pack reads for two publication IDs in one final cursor validation. This does not justify caching permission results: publication materialization also validates live ACL, privacy, dependency closure and optional topic/facet visibility. No cache/authority shortcut was introduced. These interim measurements do not replace final same-hash tests or a fair same-input legacy benchmark.

Before deployment, inventory only metadata using a read-only connection on the authorized target, for example:

```sql
SELECT id, workflow, workflow_engine, graph_version, state_schema_version,
       status, input_hash
FROM agent_runs
WHERE status NOT IN ('succeeded','partially_succeeded','failed','budget_stopped','cancelled');
SELECT id, workflow_id, workflow_type, graph_version, state_schema_version,
       status, goal_binding_hash
FROM workflow_executions
WHERE status NOT IN ('completed','failed','cancelled');
```

Do not export input manifests, bodies, or evaluation cases to perform this inventory. Separately count original ordinary-checker/feedback-target creation contracts using their metadata and exact original deployment identity. Strict source identity changes can affect ordinary checker contracts as well as replay and target feedback. Unknown versions, old source/profile bindings and `research` without a handler require their original verified environment or an explicit blocker, not silent fallback. No target production inventory was executed here; its counts remain unknown.

## 原始隐私代次栅栏

新版本的 `evolution_graph.privacy_generations` 在付费 Run 准入时一次性冻结，覆盖发起人、私人知识域所有者和实际可信依赖的私人所有者。该映射属于原 `run.input_hash` 与请求的 `admission_manifest_hash`，不是从当前状态补齐的恢复提示。缺失、错误类型（包括布尔值）、漏所有者或代次变化必须拒绝。候选、回放与零模型调用的回放臂都适用。

依赖所有者来自明确的冻结引用及其阻断闭包；知识包的可选 topic/facet 作为独立引用处理。各包保留原 1024 根上限，不将所有案例拼成更大的授权操作。后续实际模型、选择清单和私有臂工件的依赖所有者必须属于原冻结集合。费用锁等待后、外发前、返回正文保存前、领域提交和发布对账均重验当前代次，继续使用原共享隐私锁和独立擦除栅栏协议。旧未标记运行与原模型请求哈希算法不变。
