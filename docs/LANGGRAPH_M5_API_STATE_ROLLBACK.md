# M5 API, state, migration and rollback

## Same-conversation commands

`POST /api/v1/conversations/{conversation_id}/messages` retains the old message form and adds a new caller-only form. The marker is **required**, not inferred from ordinary text:

```json
{
  "contract": "practice-dialogue-v1",
  "text": "submit_practice",
  "idempotency_key": "explicit-submit-1",
  "expected_focus_revision": 4,
  "workflow_id": "the-existing-workflow",
  "execution_id": "the-practice-capable-learning-execution",
  "expected_workflow_revision": 3,
  "intent": {
    "type": "submit_practice",
    "practice_id": "practice-request-id",
    "practice_hash": "64-hex-frozen-request-hash",
    "attempt_id": "materialized-attempt-id",
    "specification_hash": "64-hex-original-exercise-hash",
    "submission": {
      "response": "  Original user answer, including whitespace.\n",
      "transfer_notes": "",
      "external_help": "unknown",
      "coding_submission_id": null
    }
  }
}
```

Replace placeholder hashes/IDs with returned real values; this is shape documentation, not a directly executable request. Authenticated identity is never taken from the payload. `text` is not parsed into an answer. All authority/target fields live in the typed caller envelope, not coordinator actions.

One intent per message:

| Intent | Required intent data | Effect |
|---|---|---|
| `start_practice` | `activity` (existing ActivityInput), `assistance_mode`, `expected_profile_revision`, `module_hash` | Admit one durable request; graph creates activity/attempt later. Explicit request overrides only the domain's existing voluntary activity opt-out, not permissions. |
| `submit_practice` | Exact practice target tuple plus original `submission` | Freeze user answer, seal workspace; no automatic paid assessment. |
| `request_assessment` | Target tuple and `submission_hash` | New assessment request for exact frozen answer; outer `authorization_ref` optional. |
| `authorize_assessment_request` | `assessment_request_id`, `request_hash`; outer `authorization_ref` | Seal the exact previously pending request once. |
| `record_practice_observation` | Target tuple and original `observation` | Original self/human review rules; model candidates are not accepted here. |
| `record_tutor_feedback` | Target tuple and `feedback` {turn_id,signal,content,consent=false} | Existing real-Tutor feedback domain, default no Experience. |
| `acknowledge_module` | Target tuple and original `acknowledgment` (SessionModuleAckInput) | Explicit original delivery/acceptance ACK, never automatic mastery. |
| `end_practice` | Target tuple | Finish submitted practice without fabricating feedback or score. |
| `abandon_practice` | Target tuple | Close an unsubmitted attempt and seal its own workspace; preserve evidence. |

Target tuple: `practice_id`, `practice_hash`, `attempt_id`, `specification_hash`. Funding is allowed only on explicit assessment commands. A pending/unknown assessment must be settled or the whole workflow explicitly cancelled before ending its practice. Existing workflow pause/resume/cancel APIs remain available.

`GET /api/v1/conversations/{conversation_id}/practices/{practice_id}` returns validated practice/status references, immutable exercise specification, submission hash, assessment request and (after validated completion) candidate. Creation responses are immutable admission receipts and may have null activity/attempt IDs; fetch the actual status after the graph effect. Never mutate old admission receipts with later results.

Known rejections include `PRACTICE_CALLER_CONTRACT`, `PRACTICE_ACTIVE`, `PRACTICE_BINDING`, `PRACTICE_ANSWER_REQUIRED`, `ASSESSMENT_PENDING`, `ASSESSMENT_RECONCILIATION`, `REVISION_CONFLICT`, `PRACTICE_ASSESSMENT_REQUIRED`, `PRACTICE_ACK_REQUIRED`. Action-level failures appear in `action_receipts`; unknown foreign funding fails before any new message mutation.

## Reference-only State Schema 1

Session tuple: `(learning_session, langgraph, learning-session-practice-v1, 1)`.
State fields inherited from SessionState: execution/workflow ID, immutable binding hash, graph version, schema version, current stage. Wait payload: execution ID, kind, request ID, sequence, request kind (`practice` or `tutor`); pause additionally carries exact control command/generation. Current business state is always reread from DB. Wait kinds are `practice_answer`, `assessment_authorization`, `assessment_run`, `practice_feedback`, `practice_reconciliation`, plus M4 Tutor/selection/control waits.

Paid tuple: `(learning_assessment, learning-assessment-v1, 1)`.
State fields: workflow Run ID, graph version, schema version, current stage, result stage key. No raw answer/source, prompt, draft, credentials, balance or mastery field enters checkpoint state. Completed ModelReceipt is the durable candidate source; checkpoints are not authority.

## Migration

`cf166_practice_graph` follows `cf166_learning_graph` and adds only `workflow_practice_requests` and `workflow_assessment_requests`. Foreign keys, origin/sequence uniqueness, one-active-slot uniqueness, status/paired materialization/paired authorization CHECKs are enforced. Original domains and stored historical hashes are not backfilled. `SCHEMA_REVISION` advances accordingly. Empty and existing-schema upgrades must be run on disposable validation DBs first; real PostgreSQL tests remain mandatory for lock/crash claims.

Downgrade checks all new request histories and M5 execution/Run bindings **before** dropping either table. Retained history rejects downgrade. Do not delete records to force it.

## Safe rollback

1. Stop admitting new M5 workflows by restoring the previous explicitly approved `CAREERFORGE_LEARNING_GRAPH_VERSION` or disabling learning admission; keep global legacy default unchanged.
2. Keep the registered M5 adapter/model/schema available for already-bound executions, or cooperatively pause them. Do not reinterpret their checkpoints as v1/legacy.
3. Reconcile action receipts, creation effects, Attempt hashes, Run/ModelReceipt and funding settlement before retrying anything. Unknown external calls are not unsent calls.
4. Preserve submitted answers, feedback consent history, control receipts, source bindings and checkpoints. Close/withdraw/correct through original domain mechanisms rather than mutating frozen content.
5. Resume only under the same frozen identity and original caps/authorization deadline. New assessment requires an explicit new request after prior work is safely terminal.
6. Changed strict engine-source hashes in historical evolution tooling are not spoofed or recomputed. Keep an old deployment for historical replay where required; no M6 promotion claim is included here.

M0–M6 full acceptance is required before M7/default switching. An engineering pass alone cannot authorize that switch.
