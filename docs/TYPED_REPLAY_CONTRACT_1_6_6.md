# Typed replay compatibility and release scope

The historical `frozen-execution-replay-1.6.6` typed evaluator scores exact frozen
publications. Its executor must not inherit the default of a newer retrieval
service. It now freezes `whole-publication-typed-execution-1.6.6`, with an explicit
null retrieval-context marker and `exact-frozen-publication-1.6.6` scorer, in the
dataset, run, paired traces and report. Missing historical markers resolve to
that same contract. Unknown or partial contracts fail before admission/execution.
Existing engine implementation/hash checks remain mandatory: this compatibility
rule does not authorize resuming a run across a changed engine or rewriting old
receipts, manifests, traces or evaluations.

An unmarked historical intermediate stage retains its original input hash and
can be resumed without repeating its completed calls. The executor checks both
the stage binding and the actual retrieval result's normalization marker before
using a cached stage. A result that actually ran r3 cannot be relabeled as a
historical whole-publication result, even when its trace marker is missing.

Both arms explicitly pass that binding to the actual Coordinator/RetrievalService.
The scorer, thresholds, per-arm budgets and original regression assertion are
unchanged. Engine identity includes the entire typed module, contract fingerprint,
retrieval-context implementation and current new-admission marker. Release checks
revalidate the report/trace contract as well as the existing frozen inputs,
candidate/baseline hashes, index hashes, authority and live receipt records.

New ordinary conversations continue to admit
`question-focused-retrieval-1.6.6-r3`. Their bounded complete-fact selection, complete
conditions/support/counterevidence, source/ACL checks and exact 12,000-byte evidence
budget are unchanged. An old whole-publication replay is not evidence of benefit
under this selector. In the unchanged synthetic regression, r3 actually returned
the same three publications in each arm despite executing every authorized vector
and reranker call. Its zero improvement and inconclusive result are retained as
evidence, not relabeled as a regression in answer quality.

Historical retrieval reports explicitly state their whole-publication-only scope,
`current_retrieval_quality_validated=false`, and the missing compatible r3
execution/scoring evidence. Formal promotion or canary release cannot use this
historical contract to certify the current r3 selector, even with human Gold.
That unsupported path returns `REPLAY_DEPLOYMENT_CONTRACT`; a compatible independent
evaluation is still required. The separate additive r3 executor/scorer is documented
in `R3_RETRIEVAL_EVALUATION_1_6_6.md`. It requires explicit new dataset/schema and
contract versions; it never upgrades or relabels historical reports.

The existing `mode=test` plus `provenance=fixture` process exercise remains available.
This is not deployment permission: registration already rejects fixture datasets
outside test mode; non-test fixture evaluation is inconclusive; the shared release
gate rejects fixture production/development promotion. The compatibility guard
preserves only that isolated fixture exercise and rejects relabeled scope claims.
Synthetic scores and passing process tests are never native-model benefit evidence.
