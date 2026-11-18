# M4 opt-in Learning / Tutor execution

Default admission remains `legacy`, `knowledge-session-v1`, and `legacy-v1` model request packaging. Existing root graphs and checkpoint readers stay registered. M4 does not introduce practice/evaluation/evolution, mark modules complete, or verify competence.

For new M4 sessions set all three:

```
CAREERFORGE_WORKFLOW_ENGINE=langgraph
CAREERFORGE_SESSION_GRAPH_VERSION=knowledge-session-v2
CAREERFORGE_LEARNING_GRAPH_VERSION=learning-session-v1
```

Production additionally requires the existing PostgreSQL domain database and PostgreSQL checkpoint URL. Disabling new admission does not reinterpret existing sessions or paid runs. `context-once-v1` and explicitly unlimited build-time contracts retain their separate opt-in meaning.

## Two lifetimes and honest origins

A learning owner is a separate unpaid `WorkflowExecution`, never a dummy model Run. Automatic admission requires the real terminal `knowledge-session-v2` checkpoint and exact successful acceptance receipt, then creates generation-2 `learning-session-v1` with that predecessor. Explicit already-accepted-plan admission creates a new workflow with generation-1 learning execution, fresh authenticated admission receipt and no knowledge predecessor or invented checkpoint.

Selection drift between acceptance and the genuine Knowledge END does not leave a permanent pending-handoff mutex: immutable acceptance is retained, the original request is explicitly rejected with its scope error, and the genuine learning successor waits without creating a paid Run.

Each paid Tutor request creates one real chat Run using `learning-tutor-v1`, with actual load, reviewed-evidence retrieval, interleaved Tutor/block guard/commit, whole-question completion, delivery and result-finalization nodes. Ordinary/direct historical chat is not globally migrated.

One unresolved Tutor request per conversation includes pre-Run funding waits. Invalid/foreign grants fail before a receipt; owned unusable grants preserve acceptance and canonical question with zero Turn/Run/reservation. `authorize_tutor_request` binds the exact existing request and cannot substitute another question. Original action results never receive late Run IDs or mutable status; read the request endpoint for live state.

## Additive HTTP and same-chat controls

- `POST /conversations/{id}/learning-messages`: `LearningDialogueMessageV1`, with explicit `tutor_request` question, cap (3–25, default 8), and preferences. Original `/messages` remains valid.
- `POST /conversations/{id}/learning-sessions`: exact accepted plan/version/module, plan/profile/focus revisions, idempotency key and optional funding/question.
- `POST /workflows/{id}/learning-confirmations/{version}`: versioned acceptance question/cap/preferences.
- `POST /workflows/{id}/tutor-authorizations`: exact request ID/hash, workflow revision and new explicit funding.
- `POST /workflows/{id}/module-selection`: exact plan/version/module and plan/profile/focus/workflow CAS.
- `GET /conversations/{id}/tutor-requests/{request_id}`: current status and actual Turn/Run IDs.

Paths above have the existing `/api/v1` prefix. Same-chat free-form follow-ups remain on the same L, with monotonically ordered immutable requests and predecessor identities. `/module 2`, `/module <exact-id-or-unique-title>`, `选择模块 2` and `切换到第2个模块` explicitly select without teaching or acknowledging mastery. Append `--teach` or `并讲解` for a dependent new Tutor request. Ambiguous/missing modules do not fall through to paid teaching of the old module. Selection checks the actual locked Plan revision and active version, not only the immutable L binding. Re-accepting a plan (even the same version) requires an explicit fresh accepted-snapshot admission; replay cannot revive a stale L. Safely closed direct owners permit a new explicit task, but pause and unknown paid outcomes cannot be bypassed.

Selection cannot bypass prerequisites, mandatory checks, independent attempts or live direct/native Tutor work; it advances conversation/profile revision and clears stale module continuation state.

Pause/resume/cancel retain receipt-derived control generation and exact current-owner checkpoint verification. Unknown paid outcomes keep the request unresolved and block another attempt. A delivered Turn can finalize after active selection drifts, then its old learning owner reconciles the original request and waits for explicit selection/new accepted admission; it never rebinds its frozen plan.

## Delivery and recovery

Delivery atomically writes a server witness alongside the terminal answer event. The registered finalization-only branch validates exact request, immutable plan/goal/source, funding, privacy, policy, blocks, completion, lease, deadline, controls, independent mode and unknown outcomes. Only active-selection drift is relaxed. No model/checker call, retrieval expansion, duplicate answer event or module completion is allowed. The same worker's heartbeat and settlement switch to the witness route, not only restarted workers. The delivery witness attests what was delivered, not that the whole question was satisfied. Completion assessment and operational `unverified`/unknown outcomes remain exact; request `completed` means the child Run was reconciled, never learner mastery or curriculum completion. Existing explicit bounded gap scheduling retains its own grant/budget/source fences and cannot silently expand the Tutor Run.

Elapsed delivery latency is recorded once using database-clock start/delivery observations; unknown clock ordering is not reported as zero.

Locking is explicit: admissions and controls acquire the selected Funding before Plan/Profile; ACK resolves the true immutable funding witness before its existing Plan→Profile→Turn sequence. Finalization takes Conversation before Funding/Run and uses witness-bound, fresh immutable selection reads without mutable Plan/Profile locks. PostgreSQL concurrency remains a separate required gate, not something SQLite proves.

## Rollback and evidence

Migration `cf166_learning_graph` adds nullable admission fields and the request ledger without historical backfill. Downgrade refuses retained learning history before destructive DDL, and preflights earlier command-goal/control history for SQLite multi-revision rollback safety. Privacy export includes requests; erasure cancels them and clears payloads/checkpoints using existing ownership traversal.

Historical evolution source fingerprints are not rewritten or weakened. Source changes may safely reject replay of an old evaluation; create a separately authorized fresh evaluation rather than asserting it remains valid.

Focused synthetic tests, aggregate checks, real PostgreSQL/Redis/Celery recovery, and native business validation are separately reported in the release artifacts. No online/native acceptance or M7 clearance follows from this implementation document.
