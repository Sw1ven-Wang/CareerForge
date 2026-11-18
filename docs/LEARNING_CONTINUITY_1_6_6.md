# Learning continuity contract 1.6.6

This is an application-state and bounded-routing repair. It is not model-weight
training, a live semantic evaluation, an observation of a learner's mastery, or
proof that a client actually rendered a highlight.

## Routing

New conversation turns freeze `intent-actions-1.6.6`. A conservative interpreter
recognizes project setup, project overview, cross-project technical design, and
function learning. It separates quoted headings, negated actions, and explicitly
deferred clauses from the immediate request. Unsupported or ambiguous language
still takes the existing coordinator/clarification path. The user does not pick
a project-first/technology-first mode. Old unmarked and 1.6.2/1.6.3 routing
contracts remain unchanged.

`POST /api/v1/learning/intent` is a read-only interpretation of a question with an
optional owned conversation. It reports the learning intent, retrieval-route
intent, negated/deferred actions, uncertainty, and unchanged mastery state.
This is not a source-access grant. A comparison label does not widen an existing
hard source restriction. Multi-project content completeness remains separately
testable from the route label.

## Preference events and actual Tutor behavior

`POST /api/v1/learning/preference-events` appends a hash-bound private event with:

- A revision and idempotency key, plus set/correct/revoke operation.
- A closed style or optional-activity preference, never arbitrary instructions.
- A global, topic, source, or project-facts scope.
- A turn, conversation, or persistent lifetime. Persistent writes require
  explicit durable consent. Correction and revocation reference an existing
  owned event rather than overwriting historical records.

`GET /api/v1/learning/preference-events` exposes the audit history. `GET
/api/v1/learning/effective-preferences` previews the context-specific projection.
Ordinary whole-profile changes remain the baseline; scoped events overlay it.
A one-turn override outranks durable scoped defaults. It is consumed by a
matching submitted turn, survives durable resume as a frozen projection, and is
not copied into the next turn. An explicit "next time" event is applied on the
next matching turn rather than immediately. Conversation-only settings never
leak to another conversation. Repetition alone does not create persistence.

The bounded dialogue adapter recognizes explicit style/quiz requests, explicit
future persistence, scope corrections, and revocations. It does not infer a
permanent preference from ordinary feedback or quoted examples. Unknown or
unresolved scope corrections are not silently widened. This is a conservative
language adapter, not comprehensive language understanding. A feedback statement
without an attempt identity cannot change an unspecified exercise's help label;
a repository correction still needs an unambiguous authorized identity.

The older explicit feedback endpoint uses the same bounded adapter. It preserves
the raw feedback and independently consented training experience; training consent
does not imply durable preference consent. Feedback style changes refer to the
original turn's focus and apply on a later matching turn, never rewriting its
frozen projection or directly overwriting conversation preferences. Negative and
quoted style mentions cannot become positive preference values.

New turns freeze both the event provenance and final bounded preferences.
Tutor model requests receive only the bounded presentation controls, not the
private event text/history. Opt-out removes quiz/exercise instructions from the
actual model input as well as removing final optional actions. Architecture-first
and evidence-first add fixed server-authored organization instructions; they do
not invent missing architectural evidence. New personalized plans receive the
same scoped preference projection and separately freeze the base profile for
revision checks. Old planner receipts remain readable.

## Client navigation acknowledgments

`GET /api/v1/learning/navigation/{answer_block_id}` validates a committed answer's
exact citation and reports `server_location_valid` separately from
`client_highlight_ack`. The latter is false until a client explicitly calls
`POST /api/v1/learning/navigation-acks` with the block/citation, source snapshot,
path, full line range, event name, and idempotency key. A changed line range,
foreign owner, revoked source, or corrupt evidence is rejected. Reads and initial
navigation requests never manufacture an ACK. ACK is labeled
`client_reported_not_server_observed`; a synthetic API test is not browser QA.
The read-only status also returns the complete selected source lines and their
SHA-256, independently checked against the immutable file, so a client can display
the actual numbered text before sending an ACK. Merely returning these lines
still leaves `client_highlight_ack=false`.

## Storage, compatibility and privacy

The two new record kinds use the existing private dependency-object store and
existing idempotency table. No schema migration or historical rewrite is needed.
The current private namespace, content hash, and source closure are rechecked.
Account privacy erasure covers these records. A retained revoked preference
history is an audit record, not an active preference or a training-consent grant.

CLI exposes `learning intent`, `preference-events`, `preference-event`,
`navigation`, and `navigation-ack`. Explicit JSON input carries revisions and
consent; the CLI does not infer a client receipt merely from reading output.

## Validation boundaries

`tests/test_learning_events_v166.py` includes all 16 public step06 inputs, generic
non-fixture variations, and real service/coordinator/Tutor/plan paths backed by
an explicitly synthetic provider. It also checks revision/idempotency, cross-user
isolation, corrupted events, one-turn reversion, next-turn scheduling, scoped
correction/revocation, actual Tutor payloads, source ACK separation, and existing
runtime/assistance/mastery safeguards. It reads no judge, Gold, or holdout data.

`scripts/validate_learning_events_v166.py` exports actual public routing and
feedback state transitions. The feedback harness uses synthetic echo knowledge
and explicit fixture topic labels to isolate state propagation. It records its
limitations instead of treating that as real repository understanding. Run it
with an explicit public input directory and output file. No model credentials,
prompts from hidden evaluation, or live provider calls are required.

## Single-session plans

`POST /api/v1/learning/session-plan-proposals` adds a separate 1.6.6 input contract
with `total_minutes` and optional one-session style/activity overrides. The old
`PersonalizedPlanInput`, old idempotency operation, and model-output schemas are
not changed. A buildable request uses the existing funded planner with one frozen
call, validates the sum of all module durations before writing a plan, and
reports omitted selected publications explicitly. Overrides affect that session,
not the durable profile. A module's optional-activity restrictions remain in
force when an accepted session is used, unless the learner explicitly requests
that activity.

No reviewed knowledge or a budget shorter than the minimum module returns a
persisted `unbuildable` gap draft with no model call, no active plan, and
`can_accept=false`. Missing, unpublished or revoked publication identifiers are
rejected; no review/publication is invented. The gap can be read at
`GET /api/v1/learning/session-plan-gaps/{draft_id}` and cannot be accepted as a
learning plan.

A buildable proposal includes a relative module schedule, prerequisite IDs,
activity kinds and minute offsets. It remains a draft until the ordinary exact
plan-version/revision acceptance API is called. Acceptance activates the exact
version and first module and returns the accepted relative schedule. This is
private learning order, not an external calendar booking. Integrity, prerequisite
and total-time checks run again before acceptance; reorder/adjustment inheritance
rebuilds the schedule from the actual immutable modules.

`learning session-propose FILE` exposes the new contract. The separate
`scripts/validate_session_plans_v166.py` exercises all five public step07 requests
with an explicitly disclosed synthetic source-unit substitution. It validates
budget/consent/state mechanics, not the original public repositories' content or
plan semantics. Its outputs must not be called a full public step07 semantic pass.

## Planner model-context budget

New admissions freeze `learning-planner-context-1.6.6`. The complete authority
manifest and authoritative text catalog remain unchanged in storage. Only the
model request shares repeated group paths in `group_catalog` and repeated text
provenance in `scope_catalog`. Each original `text_id`, exact text, fact, required
depth, modality, group operator, member list and source position can be recovered
without loss. Scope references are data, not a new authority source.

Unmarked historical runs retain their exact old request shape and system text,
including already-paid receipt hashes. Unknown projection versions fail closed.
The gateway's 250,000-byte cap is unchanged. A measured public continuous-run
snapshot with three units from one source and twenty complex JD requirements
shrinks from 251,188 to 202,527 request bytes, with the same sixty access
dependencies. Oversized novel content still fails before a model reservation.
The regression test preserves the original public-derived snapshot as a size and
losslessness fixture, not as an expected semantic answer. A failed old run is not
silently rewritten; a new admission selects the new projection.
