# Observation-bound candidate selection

New JD imports and extracted goals using `jd-requirements-1.6.6` automatically enable `candidate-selection-1.6.6` when a build must discover repositories. Other new callers can opt in with `candidate_selection_version`. Explicit `source_id` and `repository_url` requests still go directly to the named source, without a selector call or extra selector budget. Unmarked historical runs retain their original first-observed candidate behavior and paid receipts.

The bounded planner still produces queries and a persistent research tree. The selector is a separate metered model request after discovery, with one call reserved before execution. It receives a server-built directory of candidate IDs, real `SearchObservation` records, frozen goal requirement IDs, and any actual research-role `SourceRead` records for that exact candidate source/version. It does not receive fabricated candidate facts, gold labels, or heldout data.

The response explicitly selects, defers, or rejects every observed candidate, and gives an ordered list of selections. Unknown IDs, missing observations, cross-candidate references, fabricated source quotations, positive mappings to `not_required` items, inconsistent decisions and over-budget choices fail closed. Metadata and path clues can justify choosing a repository for further review only as hypotheses. Source quotations are source-scoped observations, not independent review. The selector cannot publish knowledge, attest runtime execution, satisfy a requirement or update learner mastery.

The durable `candidate_selection` stage retains the original proposal, server-validated decisions, catalog hash, exact selected candidate objects and access dependencies. Replay rechecks the current observations, source ACLs, goal context and exact proposal before reusing it. Selection quality remains a model judgment requiring independent evaluation; the output always says `semantic_quality_verified=false` and `requires_independent_source_review=true`.

New build admissions also freeze `supported-topic-associations-1.6.6`. Their analysis prompt states that target topics are research goals, not compulsory labels. Units can use only supported registered topic associations. If none is supported, analysis must retain a gap rather than invent an association or force a unit. The existing minimum-one-topic unit schema and independent reviewer are unchanged. Older admitted runs without the marker keep the original analysis prompt.

## Verification boundaries

- `tests/test_candidate_selection_v166.py` checks source/requirement reference restrictions, role and namespace authorization, an explicit model-double choice overriding first-observed order, budget reservation, direct-source bypass, and historical prompt behavior.
- `harness/run_research_trial.py` operates only on public inputs and explicitly labels `fixture-smoke` versus `native-bridge`. Offline searches return only the exact recorded query response; an unrecorded query stays unknown. Materialized pinned files must match their Git blob hash before actual `SourceRead` receipts can be created.
- `--candidate-selection` exercises the real selection service. The deterministic smoke uses no extracted goal and creates no pretend requirement mappings. It establishes mechanical execution and budget/lineage retention, not ranking accuracy.
- A live native bridge call is not a production HTTP provider test and does not establish independent backend attestation, token charges or production latency. Those limits remain in its receipts.
