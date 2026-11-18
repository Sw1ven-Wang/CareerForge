# Explicit bounded knowledge builds (1.6.4)

New knowledge builds retain a 30-minute default. They accept `duration_minutes`
(strict integer, 1–10080) and `planning_call_seconds` (strict integer, 1–3600,
default 120). The latter is a configurable planning assumption, not measured
provider speed, a guaranteed upper bound, or a promise of completion. A fixed
60-second planning reserve is held for commits. Funding expiry can shorten the
actual deadline and never extends it.

API: POST `/api/v1/knowledge/builds` accepts both fields alongside `call_cap`,
`max_units_per_project` and `max_projects`. The shared-build input accepts the
same two fields. CLI exposes `--duration-minutes`, `--planning-call-seconds` and
`--max-units-per-project` on `careerforge build`.

For example, a new five-unit build may be requested explicitly with:

```text
careerforge build "Explain the fixed project and its evidence gaps" \
  --source SOURCE_ID --calls 100 --max-projects 1 --max-units-per-project 5 \
  --duration-minutes 90 --planning-call-seconds 120
```

This is a new bounded request, not a command to extend an existing run. No
paid model is invoked by displaying this example.

## Frozen planning

New admission freezes `bounded-build-execution-1.6.4` and an execution plan.
Investigation rounds and source limits fit both the authorized call cap and
wall-clock capacity under the chosen assumption. Independent review, factual
coverage and final task-sufficiency review remain mandatory reserves. One
additional quote-correction call is reserved per analysis final/source, and
one per review final/unit. Unused correction reserves are not actual calls.
Optional embedding capacity is frozen at admission too; a later provider
capability change cannot add an unplanned embedding call.

At minimum, a fixed source with five units and a JD requires 25 reserved
calls. Under a 120-second planning assumption plus the 60-second commit
reserve, it requires 51 minutes. A 30-minute request for that shape returns
`BUILD_TIME_BUDGET` before any model call or funding reservation. The error
names the public parameters that can be adjusted. A smaller `call_cap` can
instead produce `BUILD_BUDGET`. Lowering the planning assumption is explicit
and does not make a slow provider complete faster.

A new outgoing call is also refused if the actual remaining deadline is less
than the frozen per-call assumption plus commit reserve. Exact completed
receipt replay does not spend another call and does not use this new-dispatch
check. Existing lease, funding, role, source and privacy checks still apply.
The deadline fences further business writes; an already issued remote
request cannot be recalled or promised to stop exactly at that instant.

## Recovery and compatibility

A lost worker lease can recover with a new epoch before the original deadline.
The deadline is never extended. Expired or cancelled runs cannot be restarted
by retrying the same key. A matching retry returns the original run without
changing its manifest, receipts, billing or deadline. Old admitted builds
continue their original investigation/call contract; omission of the new
parameters permits a same-key retry. Adding explicit new execution parameters
to an old key is an idempotency conflict and requires a new key.

Shared singleflight identity includes the frozen duration and planning
contract. It cannot reuse a task with different execution assumptions.

## Checkpoints are not publication

GET `/api/v1/runs/{id}` returns the actual `deadline_at`, execution plan and a
read-only `checkpoint` alongside the original result. The checkpoint reports
saved analysis units, committed reviews, accepted-but-unpublished reviews and
actual publications separately. It makes no task-completion claim and is
revalidated against current namespace/goal/source/dependency access, immutable
unit/review hashes and the review's actual source reads. Shared
subscribers see it only through their verified progress rights.
Each verified child shows its run ID, workflow, status and, if available, a
bounded machine code from its latest matching terminal event. Missing or
invalid terminal codes are null. Raw error/event payloads are not included.

The existing root `partially_succeeded` state can describe partial workflow
artifacts even when no knowledge was published. Read `publication_ids`, child
failure gaps and `task_coverage.complete` separately; that state alone is not a
claim of delivered knowledge or complete task coverage.

The existing publication guard is unchanged: only a currently leased root can
publish, with its successful same-parent analysis and review children, exact
version/hash matching, independent evidence and full factual-coverage gates.
A failed later unit does not delete previously committed review checkpoints,
but an accepted checkpoint is not promoted to a publication. No stale worker
writes a successful result after deadline. Per-unit publication remains a
separate workflow design, not part of this repair.

## Validation limits

Regression tests use explicit local fixture models and temporary databases.
They establish contract, accounting, authorization, replay and timeout fences,
not real-model quality or production service latency. The historical primary
30-minute runs and independent Mini90 supplement remain unchanged evidence.
