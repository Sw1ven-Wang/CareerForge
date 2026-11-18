# Rendered-answer completeness

Newly submitted turns carry the server-owned marker
`answer_completion_contract: rendered-answer-completion-1.6.6` in both their
frozen focus and run input. Public TurnInput/HTTP/CLI schemas do not expose a
switch: unknown fields, including a null, old or future marker, are rejected.
An old conversation focus cannot override the admission marker. Historical
already-admitted turns with no marker in both records retain their exact prior
requests, budgets and receipt replay; this is not an admission option.

Factual support and original-question completeness are different gates. The
existing Tutor and independent fact checks still permit only accepted FactAtoms
and publications. High-risk paraphrases still fall back to the exact reviewed
fact; supporting quotes do not become new facts without normal source review
and publication. Tutor `complete` remains only a generation stop request.

After factual tutoring, a separate independent check evaluates the original
question against the actual committed answer text. It receives the accepted
blocks and their reviewed facts, not the earlier Tutor draft or citation-only
claims. The immutable assessment binds the original UTF-8 question hash,
selection manifest/scope, actual block IDs/content hashes, publication versions
and facts. Publication authorization and immutable block contents are checked
before and after the model call and again before transport completion.

The completeness request uses the existing `answer_checker` role and the new
`AnswerCompletionCheck` schema: complete/partial/unknown, covered_fact_ids,
missing_aspects and reasons. It may not invent anchors or publish facts. The
check judges only what the user actually asked. A concise direct answer to a
narrow static question needs no unrequested whole-repository, runtime,
performance or learner-mastery proof.

One checker call is reserved **inside** the existing eight-call chat cap. No cap
or funding is enlarged. Navigation, action/refusal paths and answers already
carrying an evidence/operational gap do not incur this additional call. A tight
budget can therefore permit fewer factual blocks; this is disclosed instead of
claiming completeness. Budget, authorization or malformed-verification failures
are operational `unverified` outcomes, not knowledge-build gaps. Unknown outcome
receipts are not retried as new calls.

Partial/unknown independent coverage preserves the accepted facts and appends a
server-written evidence-gap notice. The existing KnowledgeGap mechanism records
the original turn so authorized exact-question analysis, separate source review,
new publication, re-answer and reconciliation can proceed. This change itself
does not schedule a build or alter any historical fact or publication.

The terminal stage is
`answer.completion:rendered-answer-completion-1.6.6`. The run result and
`answer.completed` event distinguish delivery from `task_satisfied`:

- complete: true, supported by the separately bound assessment;
- partial/unknown: false, with an explicit evidence gap and original question;
- unverified: null, with an operational verification notice and no manufactured
  knowledge-build gap.

The stage is persisted before the gap block. Crash recovery reuses the paid
receipt or completed assessment, does not regenerate accepted text, and never
duplicates the terminal notice. The result and schedulable evidence KnowledgeGap
commit in the same transaction as terminal delivery, so a crash immediately
after delivery cannot lose the next step. Old frozen candidates, native evidence and paid
model receipts are not rewritten. Synthetic protocol tests are not native-model
quality or evidence of successfully closing a real knowledge gap.

## Evidence-only turns without an AgentRun

The no-funding factual path makes no model calls and cannot claim independently
verified question completeness. It retains the exact reviewed facts and reports
`answer_coverage_status: unverified` with `task_satisfied: null` and an operational
verification notice. Existing evidence gaps instead retain their evidence status
and may create a KnowledgeGap. No fake AgentRun, model receipt, or StageArtifact
is created for this path.

Before its first block commit, this new-contract path freezes the deterministic
checked block plan in the existing server-owned Turn JSON. The plan binds the
turn, original question hash, privacy generation and selection manifest. After
all planned blocks are accepted, the bound assessment is saved in the same
server-only state before the terminal suffix. This field is not accepted by
public TurnInput/HTTP/CLI, and admission discards any inherited conversation
value. The ordinary public outcome fields and contract marker are unchanged;
there is no schema migration.

An interrupted same-key request reuses that plan and manifest rather than
rerouting, retrieving again, or passing the suffix into the Tutor cursor.
Original-question, source and privacy checks still apply. Each block and final
commit holds the original privacy-generation fence in its own transaction.
Terminal delivery atomically records the outcome event and any genuine evidence
KnowledgeGap. Operational verification failure never creates a build gap.

Isolated service/API tests cover crashes before and after plan persistence,
after the first and last fact commits, during assessment persistence (rollback),
after assessment before suffix, after suffix before finish, and immediately
after final delivery. They also cover repeated HTTP retry, direct finish without
an assessment, source revocation, question changes, privacy changes and
cancellation. This is bounded evidence-only answer durability, not a claim that
all navigation/actions or downstream observation/focus bookkeeping are atomic.
Already-admitted unmarked legacy turns retain their original no-run semantics.

The newer server-marked evidence-only continuity contract also atomically commits
the original focus projection and existing rollout observation with delivery.
Its admission/compatibility boundary and complete tail-effect inventory are in
`ANSWER_CONTINUITY_1_6_6.md`; older unmarked plans are not rewritten.
