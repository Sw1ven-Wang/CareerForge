# Learning presentation repair

This additive contract repair fixes information loss between a personalized proposal, a stored plan, an activity, and its history. It does not certify model quality, code execution, source capabilities, or learner competence.

## New bounded projection

The planner receives an authorized `text_catalog` built from the already-frozen request, profile context, goal, and selected reviewed publications. Each text reference contains a catalog `text_id` and an exact `quote` of at most 600 characters. Module title excerpts are additionally limited to 120 characters. Excerpts must come from the selected publication and focused facts where applicable. Unknown IDs, fabricated text, control characters, and instruction-spoofing patterns fail closed.

A module may specify `task` with a closed activity kind, a source-anchored title, up to eight input excerpts, up to six hypothetical scenarios, up to eight boundary excerpts, and up to six observable acceptance checks. Scenarios use closed operations: an as-given case, equality/below/above a boundary, or a bounded exception sequence followed by return/raise. Repetition is limited to 1–20 and a sequence symbol must be a single identifier found in authorized text. These are proposed inputs, not claims that execution occurred or that a repository implements the hypothetical behavior.

Observable checks bind a permitted rubric key, target excerpts, and optional scenario indexes. The server renders their instructions. A partial quotation is explicitly not a standalone factual claim. Complete focused facts, their conditions, and fixed-version citations remain attached to the module. All task and plan competence flags remain false. Runtime criteria still require a separately authorized exercise and actual bound receipts.

New goal gaps use a goal/request reference, a closed reason code, and optional reviewed evidence references. Raw planner prose is never returned as user-facing gap text. The result and plan detail include projected, unprojected, and deduplicated counts.

Providers that still omit `task` receive a deterministic fallback scoped to the selected facts and current authorized request. The fallback does not invent a task-specific fact or an observed result. It does not add assessments when checks are off. Reading-only task labels cannot conceal an assessment or scenario, and diagnostic/check/exercise preferences remain independently enforced.

## End-to-end retention and authority

- Proposal results and each plan-detail version expose validated uncovered goals.
- Module task data survives activity creation; the activity prompt and frozen rubric retain its specific inputs, scenarios, boundaries, and observable checks. Default activity rubric keys cannot silently drop the module's specific criteria.
- A narrowed activity fact selection may not discard facts used by its frozen task, and it may not widen a module's fact scope.
- Adjustments preserve the concrete task and gaps; reorders preserve the proposal manifest and must obey the prerequisite DAG. Reduced-session adjustments stay within the current session cap.
- `learning plan` displays the complete detail. `learning gaps` gives a GET-only summary of validated gap projections. CLI resume reads the same protected run-result API.
- Plan detail, acceptance, activity use, and learning run-result reads recheck manifest integrity, module hashes, the selection closure, current fixed publications, and any saved goal. Tampering or revoked evidence blocks dependent display/use, rather than exposing cached source/goal excerpts.

## Record compatibility

New personalized module, plan-projection, draft, and activity JSON contracts have explicit 1.6.1 tags. No SQL columns or migration are added. Original 1.6 manifests and modules are not rewritten. Revisions retain their original draft format while using the new manifest integrity envelope.

Old missing-goal strings only select matching, bounded excerpts from the original authorized goal/request context. They are not echoed. Unsupported, oversized, or empty old strings are counted as unprojectable; duplicate recovered goal excerpts are combined. This intentionally recovers less prose than the old raw draft contains, while keeping valid old modules readable. A missing old manifest does not acquire invented provenance, and unknown new schema versions are rejected.

The text-safety checks supplement exact source anchoring and structured templates. They are not a general semantic verifier, nor do lexical matches prove coverage. Future model-quality evaluation remains separate from the synthetic service, authorization, and compatibility regressions.
