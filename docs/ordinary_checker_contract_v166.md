# Ordinary conversation checker contract

New conversations created by the existing `POST /api/v1/conversations` route
adopt `question-scoped-citation-checker-1.6.6-v1`. The existing creation and turn
request DTOs are unchanged. Ordinary feedback and continuation continue through
the ordinary turn route; they never create a learning session or select a module.

Existing unmarked conversations retain their original behavior. An owner can
explicitly adopt the version for **future turns** with:

`POST /api/v2/conversations/{conversation_id}/checker-contract-adoptions`

The new strict DTO requires `expected_revision`, the exact `contract` above,
and `idempotency_key`. Adoption rejects a nonowner, stale revision, active queued
or running turn, or a reused key with a different complete request. The owner and
conversation locks serialize concurrent admission/adoption. Repeated identical
adoption returns the original result without advancing the revision twice.

## Immutable admission and compatibility

A server-owned object and independent idempotency receipt record each conversation
admission. A separate object and receipt freeze each new ordinary turn's version,
implementation identity, owner, namespace, privacy generation, original question
hash, request hash, funding identity and revision. The run manifest carries the
same token. The same snapshot freezes the server-admitted `answer_contract` and
`answer_completion_contract`, including for turns without a funded run. Removing,
nulling or changing either value cannot turn a newly admitted request into a
legacy request. Deterministic object lookup detects removed or changed markers, even
when both turn and run markers are stripped. Read-only history validates the
original snapshot rather than requiring the current implementation.

New turn snapshots additionally declare `ordinary-run-identity-1.6.6-v1`. The
original admission transaction seals a separate `conversation.ordinary_run`
idempotency authority under the actual run ID, binding its turn, full token and
input hash. This independent index is checked before trusting mutable workflow,
forward turn hints or `Turn.run_id`. Simultaneously stripping those hints or
reclassifying a run cannot bypass its adopted contract. This is defense against
corrupted server records, not a claim that a public API can edit payer/workflow.
No old unmarked run is given a new authority record. Earlier marked completed
history without this later witness remains read-only under its original identity;
pending marked work cannot silently use a different implementation.

Old queued turns and paid checker receipts are not upgraded by changing the parent
conversation. No background migration adds markers or changes paid request bodies.
The old unmarked checker wire stays `question`, `proposed_block`, and `facts`.
A changed implementation cannot silently execute an already marked pending request.

The new contract identity participates in retrieval caching, selection manifests,
answer plans, no-funding answer plans, checker stage keys and evolution engine
identity. Legacy batch answer caches cannot satisfy a new contract. No old
evaluation constitutes approval of this new implementation.

For an evidence-only factual answer, the selection binding and its immutable
answer plan commit in one transaction. A crash before that transaction commits
leaves neither binding nor plan; recovery may retrieve again. Once the plan is
committed, recovery reuses the original selection and cannot replace its scope.
Funded turns retain their existing durable retrieval-stage and paid-receipt
recovery contract.

## Body-free invalidation delivery

Execution and committed-body reads reject an invalid ordinary admission or
changed privacy generation. The event read route additionally preserves a safe
terminal view for the current authorized owner. Only an independently recorded
ordinary admission and the two recognized binding/privacy-generation errors can
use this view. Current owner, namespace access and unblocked privacy are checked
again under the turn lock; unknown errors and access or erasure failures are not
converted into events.

The view contains only persistent, idempotent `answer.evidence_invalidated` and
`answer.interrupted` notices with a fixed code, `redacted: true`, and block IDs
belonging to this turn. Old payloads, text, questions and hashes are not copied.
It reports a `blocked` read status without changing historical execution status,
completion records, blocks, receipts, funding, stages or gaps. Reconnection after
the former last event sees the new notices, and subsequent reads do not duplicate
them. Valid historical blocks after funding withdrawal keep their earlier read
semantics.

## Evidence and authority

Ordinary checker context contains `question_scope`, never `session_scope`.
It binds the original current question (or its server-resolved continuation),
current selection manifest, and a single reviewed fact with its claim. It does
not include a plan, module, JD, goals, or unrelated private history.

The shared evidence projector requires the current standalone/isolated fact
contract and complete support plus counterevidence. Quotes, hashes, repository,
commit, file path, line locators and read witnesses come from fresh authorized
objects. Caller or model source assertions are not authority. Complete UTF-8
context size is checked without truncating evidence. Missing, mismatched,
revoked or oversized evidence fails closed.

The gateway rechecks current ACL/privacy/source permissions, payer/funding,
roles and data classes before dispatch and when accepting the result or replaying
a paid receipt. A new ordinary contract rejects a changed payer or funding
identity and redacts in-flight responses when funding is withdrawn. Private
source content is declared `private_source`, with its direct dependencies.

Code display checks prove only a byte relationship to a complete citation.
They are not proof of semantic support, equivalent execution, or an enlarged
claim scope. Independent negative semantic verdicts remain negative.

## Validation boundary

Synthetic protocol tests cover permissions, concurrent adoption, full-request
idempotency, marker tampering, no-funding replay, ordinary feedback scope, and
fresh evidence. A separate cross-code test creates databases with a real legacy
checkout in another process, then checks queued requests, existing paid receipts
and completed result replay using the candidate, including no-run crashes before
and after plan persistence. This real C3 source already includes completion; it
proves compatibility with unmarked ordinary turns, not a pre-completion release.
A separately labeled synthetic persisted-state fixture exercises the older
missing-completion-marker format without first admitting a new ordinary turn.
Neither test category is a native-model evaluation or production release approval.
