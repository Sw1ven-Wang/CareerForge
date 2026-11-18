# Explicit accepted-session Tutor binding

This is an opt-in additive contract, `accepted-session-tutor-1.6.6-v1`. It neither
changes the original TurnInput nor implicitly binds ordinary conversation turns.
It does not publish knowledge, promote policies, complete a module or establish mastery.

## Public entry and selection

POST `/api/v1/conversations/{id}/session-tutor-turns` accepts
`SessionTutorTurnInput`: the existing question, expected_revision, optional
funding_id and idempotency_key, plus required namespace_id, plan_id, version_id,
module_id, expected_plan_revision and optional typed SessionPreferences. The new
model_call_cap is a strict integer from 3 through 25, defaulting to 8.

The user must select these identifiers for **each** request. The server requires
ownership of the conversation and current private namespace, an accepted
single-session plan, its exact active version and the profile's active module.
A draft, an ordinary non-session plan, a different plan/version/module, stale
revision or an unbuildable no-knowledge draft cannot authorize Tutor. A grant to
another person's namespace does not grant ownership of their plan/conversation.
Module publications can use separately granted source access, which is rechecked.

`careerforge learning session-tutor CONVERSATION FILE` is a thin explicit JSON
client. `learning session-tutor-binding TURN` reads the same snapshot as GET
`/api/v1/turns/{id}/session-tutor-binding`. No scope, funding, consent or revision
is invented by the CLI. There is no separate packaged SDK in this repository.

## Frozen audit identity and provider content

A private selection_manifest freezes the request, full-request hash, turn and
conversation identity, privacy generation, plan-manifest hash, module-content
hash, effective preferences and their sources, and an exact reviewed fact/support
projection. Its token is exactly `{contract, manifest_id, snapshot_hash,
request_hash}`. The token is pinned in the Turn, chat run and a separate immutable
idempotency record. Removing the marker cannot downgrade a no-model turn.

The read endpoint exposes the bounded module projection, effective preferences
and preference_sources, token, turn/run IDs and learning_session_id. A real immutable
learning_session is created for every bound reading/teaching activity and depends
on that exact selection; it is not an exercise. The provider sees
`context.focus.session_module` containing the same plan/version/module IDs,
revisions, module_content_hash, timings, closed step codes and server-owned
teaching-step text, and **only fact IDs present in that actual provider call**.
It also sees effective_preferences and preference_sources. It never receives the
raw plan, goal, old conversation history or omitted-module text through this
contract. This is real planned teaching content, not just unused database IDs.

The immutable plan selects only existing independently reviewed standalone
FactAtoms from its module publication. The new flow bypasses general search and
coordinator suggestions and rebuilds this exact selection with full support and
counterevidence. It does not enlarge search budgets or weaken r3/r4 retrieval or
standalone-consumption contracts. Oversized complete module evidence fails closed;
no fact or support is clipped. Unmarked legacy chat keeps its exact 8-call ceiling.
The new request lets the user explicitly choose a bounded call cap (3..25, default
8), which is frozen into its full-request idempotency hash and the funded run. The
entire cap must fit existing authorized total funding. Tutor/checker role permissions,
actual source scope, and learning_state plus source data classes are checked before
admission, then again at every existing reservation/dispatch/replay boundary.

A module selects at most 12 facts. The frozen budget reports fact_count,
requested_model_call_cap, model_calls_authorized, maximum_fact_steps and
ideal_minimum_calls_without_rework=2N+1: one Tutor call and one independent block
check per fact, plus one terminal semantic assessment. This is an ideal minimum,
not an upper bound or promise. meets_ideal_minimum is only arithmetic and
coverage_guaranteed is always false. Rejection, retry, early empty complete,
timeouts and deadline/permission changes may leave gaps. The loop ends when all
selected facts are actually delivered, even if the last draft says complete=false;
it then obtains the independent terminal assessment instead of spending another
call asking for an empty complete. A model's early empty complete never erases
undelivered_module_fact_ids. With 4 or 5 facts and default cap 8, at most 3 fact
steps are planned; missing facts and the budget gap remain explicit. Terminal
assessment can be deterministic unverified in an operational-gap case, without a
further paid semantic call. delivery.model_call_usage reports actual authorized_cap,
consumed and held counts. User ACK preserves these values and all known gaps.

`session_module` is private learning_state, even when its evidence is public.
Evidence contributes actual public_source/private_source classes. Existing
funding roles, class and source allowlists, dependency ACLs, receipt identity,
privacy generations and cancellation/lease rules all remain required. The actual
Tutor request declares the binding manifest as a dependency. Answer blocks have
blocking `bound_to_session` edges; the manifest has a blocking module edge.
The completion assessment's binding includes the same session_tutor_binding,
and both its binding_hash and assessment_hash therefore pin the selected session.
The final result/terminal event also carries this token and explicit false flags
for module_completed and competence_verified.

## Preference order and one-turn scope

Highest priority first:
1. Typed preferences explicitly supplied in this Tutor request.
2. Applicable explicit conversation/turn lifetime preference events.
3. Explicit preferences in the accepted session's original session_request.
4. Current persistent/topic/source/default presentation preferences.

Each effective key has an auditable preference_sources entry. Mere default values
stored in Conversation.preferences are not promoted into explicit scoped events
and cannot override a session's practice=false or requested_style choice. A turn
lifetime event is consumed by the existing applied_event_ids contract and does
not leak into the next bound request. Effective preferences are frozen for the
accepted request and exact replay. Neither profile/Conversation preferences nor
long-term settings are written. The question is not parsed into durable preference
writes on this opt-in path. Use the typed field for this request or the existing
explicit preference-event endpoint for a different scope.

## Fresh authority at every boundary

Submission and exact-key replay, execution/resume, runtime lease validation,
model reservation, pre-dispatch, returned-body persistence, every answer commit,
completion check and terminal success validate the binding. Checks include current
plan/version/revision/module, exact module/manifest hashes, original privacy
generation and dependency ACL/state. Replacing the active plan or version,
revoking source access or evidence, cancellation, funding withdrawal and erasure
cannot be bypassed by a cached provider receipt. A body returned after replacement
is redacted, not committed. Historical read/reconnect paths validate original identity/hashes and current
source permissions/privacy, but do not require the module to remain active. A
completed answer stays readable after explicit advancement or a later plan version.
New execution, continuation and first module ACK still require current active state.

The new endpoint's idempotency compares the **complete** typed request, including
funding, revisions, model_call_cap and preferences. Its key cannot replay an ordinary TurnInput
request and vice versa. Old TurnInput schemas, old input validation and unmarked
provider request bytes are preserved. Engine/canary identity hashes include all
new binding and preference code, so past evaluations cannot silently certify it.

## Explicit teaching-session ACK and progression

GET `/api/v1/learning/teaching-sessions/{id}` / `conversations.teaching_session`
returns the persisted teaching session, actual delivery status, final block hashes,
completion assessment hash, delivered/undelivered fact IDs and a delivery_hash.
It also exposes current_binding_active, can_acknowledge_module and current profile
revision; history remains readable without presenting an old module as active.
ack_requirements exposes mandatory acceptance blockers, active independent work,
any required submitted attempt kind and whether explicit incomplete acceptance is
needed. can_acknowledge_module means eligibility to request, not verified acceptance;
it is false for mandatory-blocked or active independent-workspace cases.
No transport completion or model `complete` automatically changes module progress.

POST `/api/v1/learning/session-module-acks` / `conversations.acknowledge_module`
accepts SessionModuleAckInput. It requires learning_session_id, turn_id, namespace_id,
plan_id, version_id, module_id, strict expected_plan_revision and
expected_profile_revision, binding_snapshot_hash, completion_assessment_hash,
delivery_hash, `confirm=ACKNOWLEDGE_MODULE_AND_ADVANCE`, an idempotency_key, optional
attempt_id and strict accept_incomplete_material (default false).

First confirmation requires the exact current accepted plan/version/active module,
a completed real delivery with at least one reviewed module fact, and consistent
final hashes. Partial, unknown, unverified or omitted module material requires
separate explicit accept_incomplete_material=true. The receipt preserves those
original gaps and verdicts; it never changes them into acceptance success.

The transaction records an immutable module_completion_ack with blocking links to
the teaching session and each delivered answer block, then advances active_module_id
to the next module in the accepted version's fixed order. The last module sets it
to null. Profile revision is incremented once. plan_progress_status is
next_module_active or finished_by_user_ack. module_completed=true in this receipt
means only explicit user acknowledgment: completion_basis is
explicit_user_confirmation_not_competence, acceptance_passed=null and
competence_verified=false. The old Tutor output's false flags remain its historical
statement that answering alone did not complete anything.

Same key plus identical full request returns the original receipt without requiring
the module still be active or advancing again. It still validates original hashes,
source ACL/state and privacy. ACK first checks the true immutable session identity
without locking the caller-selected plan, then locks only that matching plan; an
incorrect but otherwise owned plan cannot introduce a reverse lock order.
Different payloads conflict; a different key cannot
acknowledge the same session twice. A new key cannot advance an old version/module.
The service acquires Plan then Profile then Turn locks. Existing plan acceptance
now locks and refreshes Profile after Plan before incrementing its revision, so
accepting a different plan cannot overwrite a concurrent ACK revision. Profile
updates and preference events likewise lock and refresh Profile before validating
and incrementing its revision. Erasure-namespace reinitialization also refreshes
under this row lock; the existing personal-access privacy fence remains required. New no-model completion
paths acquire the same authority locks before Turn; funded paths first acquire
Run. ACK does not acquire later Run/Principal/Conversation write locks. SQLite
races are tested; this is not a claim of a real PostgreSQL concurrency run.

A reading module never creates an exercise. A separately requested existing
LearningActivity can use the same module_content_hash/publication/fact scope.
Non-reading planned work requires a real submitted same-module, same-kind
LearningAttempt; its state is preserved as submitted_not_automatically_verified.
A generic ACK cannot bypass mandatory=true acceptance criteria: this version
refuses that path rather than inventing an evaluator. Existing source-navigation
ACKs remain client-reported display receipts, are valid historical reads, and
cannot substitute for this explicit current-module confirmation.

The API authenticates the user in the existing actor dependency. Client fields
claiming human/synthetic/evaluator/competence authority are rejected. Server test
mode records confirmation_provenance=synthetic_user_confirmation. Native experiments
that simulate a user must explicitly use test mode while retaining their real
provider and allow_fixture_publication=false. Other authenticated deployments use
authenticated_user_confirmation. Synthetic ACKs never prove real user approval.

CLI additions are `learning teaching-session ID` and `learning module-ack FILE`.
No module progression is initiated automatically, by the model, or merely by
submitting a practice response. No learning-effectiveness claim is made.

## Storage and verification scope

No SQL schema migration or historical-data rewrite is needed: existing JSON
snapshots, idempotency records and dependency objects hold the new versioned
contract. The new object and edge types are additive. Privacy's existing owned-JSON
scrubbing includes these records; actual erase is covered by a contract test.
Old contract files are unchanged, and OpenAPI is regenerated with only new paths
and new input schemas. Tests are synthetic protocol/security proofs, not native
model quality, real PostgreSQL concurrency or learning-effectiveness evidence.


## Minimal explicit CLI workflow

The following commands use the existing authenticated CLI configuration. All IDs
come from the user's accepted plan and current conversation, not from model output.
Run `careerforge learning session-tutor --help` for arguments. Put the complete
SessionTutorTurnInput into `tutor.json`, including the selected namespace, accepted
plan/version/active module, current conversation/plan revisions, the user's question,
a fresh idempotency key, optional funding ID and any explicit one-turn preferences.
For funded calls the default cap is 8; include a larger model_call_cap only if the
user explicitly chooses it within the funded authorization.

```sh
careerforge learning session-tutor CONVERSATION_ID tutor.json
careerforge learning session-tutor-binding RETURNED_TURN_ID
careerforge learning teaching-session RETURNED_LEARNING_SESSION_ID
```

Wait for actual terminal delivery. Save the last GET response as `session.json`.
Review module plan/version/module identifiers, answer_coverage_status, task_satisfied,
uncovered_questions, undelivered_module_fact_ids, model_call_usage and
requires_incomplete_acceptance. `delivery_complete` only means all final blocks
and the terminal assessment are present, not that the question was fully answered.
If can_acknowledge_module is false, do not construct a fresh ACK. A mandatory task
criterion or planned practice may still require its actual permitted activity path.

Only after the authenticated user explicitly chooses to advance, construct the
request from the observed response. This snippet copies the three server hashes;
it does not recompute, fabricate or infer them. Set `accept_incomplete_material`
to true only if that user separately accepts the displayed known shortcomings.
For non-reading work, include the real submitted same-module attempt_id as well.

```python
import json
from pathlib import Path

s = json.loads(Path("session.json").read_text())
m, d = s["module"], s["delivery"]
assert s["can_acknowledge_module"]
a = {
    "learning_session_id": s["learning_session_id"],
    "turn_id": s["turn_id"],
    "namespace_id": s["namespace_id"],
    "plan_id": m["plan_id"],
    "version_id": m["version_id"],
    "module_id": m["module_id"],
    "expected_plan_revision": m["plan_revision"],
    "expected_profile_revision": s["current_profile_revision"],
    "binding_snapshot_hash": s["session_tutor_binding"]["snapshot_hash"],
    "completion_assessment_hash": d["completion_assessment_hash"],
    "delivery_hash": d["delivery_hash"],
    "confirm": "ACKNOWLEDGE_MODULE_AND_ADVANCE",
    "accept_incomplete_material": False,
    "idempotency_key": "USER_CHOSEN_UNIQUE_ACK_KEY",
}
Path("ack.json").write_text(json.dumps(a))
```

```sh
careerforge learning module-ack ack.json
careerforge learning teaching-session RETURNED_LEARNING_SESSION_ID
```

A successful response preserves the original gaps, specifies the next active
module or finished_by_user_ack, and reports competence_verified=false. Retrying
that same file is idempotent. A revision conflict means re-read and review the
new state rather than editing hashes to force a submission. GET requests never
produce consent. Tests exercise these exact CLI commands against the real local
HTTP handlers and label their confirmations synthetic_user_confirmation; they
are not proof that an actual user has read or accepted anything.
