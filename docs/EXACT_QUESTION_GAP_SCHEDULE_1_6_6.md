# Exact-question gap completion

New gap schedules use `exact-question-gap-schedule-1.6.6`. The server obtains the
original question verbatim from the owned, completed Turn. The frozen build input
records the gap, turn, original source scope, exact question hash, selected fixed
source, source scope hash, and call/project/unit/time limits. Admission and the
`KnowledgeGap.build_run_id` link commit in one transaction. The build remains a
normal focused knowledge build with isolated source analysis, independent source
review, safe publication and independent task sufficiency.

POST `/api/v1/knowledge/gaps/{gap_id}/schedule` requires `confirm: "BUILD"` and
accepts funding_id, idempotency_key, call_cap, source_id, max_projects (1),
max_units_per_project (1–3), duration_minutes and planning_call_seconds. Omitting
source_id is allowed only when the original gap has exactly one source. A new
unscoped or multi-source request must explicitly select an existing permitted
source; it returns `GAP_SOURCE_SCOPE` before reservation otherwise. It never
silently downloads a repository. A narrowed multi-source gap cannot be considered
fully resolved by this single-source build.

This is an intentional tightening for **new** schedules. Existing associations
with unmarked historical builds replay unchanged, including queued runs and
unknown receipts. They are not upgraded, reissued or reset. New-contract replays
must retain exactly the same caller request and original server-side provenance;
changed source or limits return `GAP_SCHEDULE_CONFLICT`.

The HTTP endpoint always supplies the exact-question contract marker, including
when the client omits it. Unknown markers are rejected. Only the unversioned
six-argument internal call can preserve the old label-based schedule for a gap
without an original Turn or marked build. Any new option opts into the strict
contract. A damaged origin, missing link to a recorded marked build, or a future
or null stored marker cannot downgrade to legacy behavior. The legacy path keeps
normal budget/idempotency admission and checks privacy generation both inside
build admission and before linking the gap. Its server-only generation fence is
not part of public JSON, schemas or frozen build manifests.

Calls reserved at admission are held, not consumed. Ordinary Runtime accounting
records actual calls, returns unused allocation and settles terminal budgets.
There is no funding increase or budget bypass in gap scheduling.

POST `/api/v1/knowledge/gaps/{gap_id}/reconcile` requires
`confirm: "RECONCILE"` and optionally an `answer_turn_id`. The first reconciliation
after a build records terminal state. A successful or partial build alone never
resolves a gap. The method only resolves when all of these hold:

- The original question has complete coverage recomputed from this build's
  committed, non-synthetic independent sufficiency receipt and published anchors.
- Publications are currently valid, source-scoped and produced by this build's
  own separate analysis and reviewer child runs.
- A new completed chat turn asks the exact original question, under the same
  principal, namespace and hard source scope, and its retrieval retains that scope.
- The answer contains independently checked non-synthetic facts, uses a new
  publication and fact from this build, and contains no evidence-gap block.

Incomplete source coverage, absent execution evidence, no accepted publication,
failed builds, old answers, changed source scopes and new answers with gaps remain
unresolved. Old questions, answers, reads and model receipts are never rewritten.
Reconciliation appends a hash-bound result artifact without model calls. Existing
terminal reconciliation records replay rather than being overwritten. This is a
product lifecycle state, not an independent evaluation score or a learner-mastery
claim.
