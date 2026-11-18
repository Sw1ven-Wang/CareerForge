# 1.6.2 bounded post-blind contract repair

This patch follows the already observed 1.6.1 failures. It does not replace the
frozen 1.6.1 evaluation, change its grades, or claim new blind performance.

## Learning planner

The earlier planner advertised a flat task schema that allowed a reading task
with scenarios and acceptance checks. The server intentionally rejected that
combination. A recorded failure had all learning preferences enabled, so its
`LEARNING_OPT_OUT` result misdescribed a malformed task. The same response also
paired an observable with an incompatible rubric dimension, another invariant
that had not been represented in the model schema.

New plan requests freeze `planner_contract_version=learning-planner-1.6.2`.
Their schema has mutually exclusive literal task kinds and observable/rubric
pairs. Reading requires empty scenario and acceptance arrays. Diagnostic and
understanding tasks cannot contain transfer or runtime checks. The prompt states
these rules, the per-kind preference gates, scenario-index requirements and the
separate runtime-approval requirement. The same versioned types validate output.
Malformed output fails with `MODEL_SCHEMA`; genuine disabled activities still
fail with `LEARNING_OPT_OUT`, without changing labels or preferences.

The planner still has exactly one model call. There is no correction call,
automatic retry, extra reservation or new funding authority. Invalid output does
not save a partial plan. All source, quote, focus, prerequisite, session-duration
and inherited-activity checks remain in force.

Unmarked queued plans keep the exact 1.6.1 prompt and schema, including their
historical error behavior. Existing completed receipts can be reused without a
new call. New stored drafts use `learning-plan-draft-1.6.2`; input and draft
version markers must agree. Unknown versions fail closed. The existing 1.6.1
projection, module and activity formats are unchanged because their display
representation and inherited permissions have not changed.

## Verification routing

The known verification regression was a model decision: it selected verification
but marked the claim reference missing, chose clarification and returned
confidence below the policy threshold. The coordinator was only given source
metadata, not the specific claim text. The server correctly preserved those
three independent gates. This patch does not force that case into tutoring or
claim the original model-quality regression is resolved.

A generic paired safety test additionally exposed an existing clause-recognition
gap: a verification request followed by an editing action could discard the
second action when its verb was outside the old grammar. The bounded repair
normalizes the edit/change/modify/alter/revise/adjust/update/amend/rewrite/patch/
refactor family and usual inflections only in local action-clause detection.
It makes the rule abstain rather than executing either action. Quoted data and
identifier/path boundaries are preserved. The user's actual question, model
prompt, source selection and model authorization/confidence gates are unchanged.

New conversation turns freeze `routing_contract_version=intent-actions-1.6.2`.
Unmarked queued turns explicitly use the previous rule behavior. Unknown markers
are rejected. The coordinator's model-facing schema and prompt remain unchanged.
The existing replay engine fingerprint includes the entire orchestration module,
so this helper cannot bypass the existing engine-drift rejection. No replay
version or budget gate is relaxed.

## Validation boundaries

The patch adds generic shape, preference, observable, inheritance, exact-receipt,
queued-version, error-accounting and routing positive/negative regressions.
Synthetic service tests establish contract and safety behavior, not model quality
or human learning effectiveness. Independent engineering tests, review and fresh
model retests are recorded separately. Existing evaluation inputs, outputs and
grades remain historical evidence, not results of this patch.
