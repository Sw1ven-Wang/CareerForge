# Opt-in answer feedback continuation v1

This unactivated candidate adds one narrow presentation contract. It does not fix an old event by changing its scope, does not backfill an old conversation, and does not authorize model execution, replay, evolution or promotion.

## Explicit entry points

1. Create a **new** ordinary conversation with `POST /api/v2/conversations/answer-feedback` and an explicit `contract: "answer-feedback-continuation-1.6.6-v1"`. The usual `auto_build` and `build_call_cap` options remain available. The server freezes the complete choice and the new implementation hash in the existing ordinary conversation authority object and its independent receipt. A writable preference marker alone cannot enable the feature. There is no upgrade/adoption endpoint for existing conversations.
2. Submit questions through the existing `POST /api/v1/conversations/{id}/turns` using the unchanged `TurnInput`. The new conversation contract is server-selected; request fields cannot inject a target or a preference projection.
3. After a funded ordinary answer successfully completes with a valid fixed selection/source manifest, send `POST /api/v2/feedback/answer-continuation` with the explicit contract, `turn_id`, feedback `content`, training `consent`, and `idempotency_key`. The text is parsed by the existing bounded preference parser. This route has a separate DTO; the original feedback route and schema are unchanged.

Example of the new feedback body:

```json
{
  "contract": "answer-feedback-continuation-1.6.6-v1",
  "turn_id": "THE_COMPLETED_TARGET_TURN",
  "content": "讲得太抽象，我想直接看源码。",
  "consent": false,
  "idempotency_key": "explicit-feedback-1"
}
```

The new `answer_feedback.accepted` flag means a target adjustment was recorded. `status: pending_matching_turn` and `delivered: false` are an immutable acceptance receipt, not proof of provider delivery. Idempotent resubmission returns that same receipt. Existing `preference_update_applied` remains the legacy preference-event result and is false when only a new target binding was created.

## Exact semantics

- Only one temporary `requested_style` setting is representable. The lifetime is one admitted matching turn, and durable consent is always false. Training consent only controls the existing training Experience path.
- Unqualified feedback means the next **byte-exact question in the same conversation with the same fixed source IDs and source hashes**. No semantic similarity, deictic rewriting, global preference, inferred topic preference or cross-conversation transfer is attempted.
- Explicit named topic/source constraints are retained as additional constraints using the existing parser and eligibility rules. A named constraint is never replaced by a routing hint. Explicit exclusions, unknown names or ambiguous controls continue to clarify. Conversation/persistent lifetime, global-setting requests, multi-setting controls, correction, revocation and promotion are not silently converted to this one-use mode; use the existing preference/feedback path or clarify the choice.
- Delayed feedback is anchored to its completed target, even after navigation. Current mutable conversation focus is used only as the **new turn's frozen admission context**, never as the target's provenance. A different-source repeat of the same question is rejected before admission. Different questions and other conversations do not consume the target.
- There must be exactly one eligible pending binding. Multiple pending bindings require resolution, not a newest-record guess.
- Consumption is reserved atomically with the new Turn and its immutable projection under the principal/conversation locks. Cancellation or later failure does not refund the one-use reservation. It is not a delivery guarantee and is not retried automatically. The next admitted answer reverts to the unchanged legacy projection.

## Integrity and privacy

The target binding freezes actor, private namespace, privacy generation, conversation authority, target turn/run/question, original run input, ordinary turn authority, selection hash, source hashes, dependency-closure hash and target orchestration/result hashes. Hard topic constraints and soft hints are recorded separately under context-only metadata. They grant no source access, factual authority or durable permission.

The ordinary authority and selection checks remain intact. The new binding is an additional semantic authority, not a claim that an old ordinary binding or paid request authorized targeted feedback. Selection dependency closure and source identity/content are freshly checked. Feedback withdrawal changes the original content fence; this invalidates new consumption and pending execution regardless of training consent.

A separate immutable consumer object binds the new turn/question, entry sources, ordinary authority, complete effective projection, and target tokens. Independent turn, run and one-use receipts detect stripped markers, mismatched inputs and missing consumption receipts. Provider execution validates these records again and checks the actual new selection's sources before Tutor egress. Only the bounded style plus new target/consumer token hashes are included in the new Tutor context; source/topic metadata is not added as evidence.

Missing, malformed, conflicting, revoked or tampered matching evidence fails closed. It never becomes a global fallback or silently starts a paid recovery attempt. Privacy erasure follows the existing private-object/idempotency cleanup and generation fence.

## Compatibility and scope of validation

The original `/api/v1/feedback`, `FeedbackInput`, `TurnInput`, old preference-event payloads and `_project_preferences` remain on their old path. Existing conversations cannot acquire this behavior from a marker or the new feedback API. Completed target turns and earlier provider requests are never rewritten. Existing ordinary implementation guards still prevent executing an old paid request against changed implementation bytes.

The synthetic tests exercise a new C08-01-style integration, including the `code.python` entry-focus / `runtime.async` resolved-context split. They are not the full original FB-T02 trajectory, do not restore its missing `agent.tool_calling` initial state in a historical branch, and are not native-model quality evidence. Actual historical failure results and native-call counts are unchanged.
