# Versioned goal context for discovery planning

This candidate repairs a generic context-transfer defect, not an evaluation answer or a particular repository choice. Before the repair, BuildService froze source-bound requirements in `research_context`, but the initial research planner and subsequent reflector received only the question and topic keys. Unmapped technologies, exact depth, required/preferred/bonus/negative qualifiers, and AND/OR/NOT group paths were therefore unavailable to those model calls. The selector and later analysis already had richer requirements; they could not repair queries that were never planned.

## New admissions only

New build manifests freeze `research_planning_contract=research-planning-context-1.6.6`. Their actual discovery calls receive the exact server-frozen `research_task`, its canonical fingerprint, and the planning contract version. The initial planner and reflector use the same task. The existing ResearchTaskContract bounds its question, number of requirements, groups, and dimensions. A separate 160,000-byte task limit and the unchanged 250,000-byte full gateway request limit fail closed without dropping or truncating requirements. No additional model calls, sources, or execution budget are granted.

The server rechecks the task against the persisted stage, original question/scope, immutable goal version, JD revision, source hashes, requirement IDs, logical group paths, and access/dependency closure. A caller cannot supply a replacement task. The root tree and research result retain the task fingerprint; mismatched cached identities are rejected.

Goal context is task data, never source evidence or user mastery. Instructions retain required versus optional and negative clauses, OR alternatives, explicit and unknown depths, and the source-review/no-runtime-claim boundary. No sample JD, expected query, candidate answer, fixed repository, or fabricated source is added to production code. Queries and candidate quality still require independent evaluation.

## Privacy and external search

The initial planner, reflector, observations and research result retain the exact goal dependency. Model calls explicitly declare `private_jd`, in addition to their existing namespace/policy data classes. The goal dependency transitively binds its imported JD. Revocation, erasure and integrity changes remain fenced by the existing runtime and model gateway checks.

For marked runs, GitHub search and release discovery also require the goal dependency and `private_jd` funding permission. The provider rechecks them after HTTP completion and will not accept a response after revocation or permission loss. Such goal-dependent queries cannot populate the public-only HTTP cache. The existing production provider dispatch and budget path is used; tests use httpx.MockTransport and do not contact GitHub.

## Compatibility

Unmarked historical runs retain their original planner/reflector system text, context keys, QueryPlan schema, dependencies, data classes, tree identity and paid request hashes. Existing queued runs are not upgraded by resubmitting their idempotency key. Supplying a new task to an unmarked run is rejected. An unknown new planning version fails closed. Direct `source_id` and `repository_url` builds still bypass discovery; this repair does not manufacture a source for a fixed-source review request.

No existing research, JD extraction, candidate selection, analysis, review, replay, or public API schema is rewritten. No database migration is needed.

## Verification

`tests/test_research_goal_context_v166.py` adds 31 deterministic cases. The actual BuildService → ResearchService path is exercised with seven independently source-bound fixture requirements and no topic mapping, followed by a real reflector call. Coverage includes exact qualifiers and identities, new imported-revision discovery, call/byte bounds, changed or missing inputs, persisted tampering, goal/JD revocation, private-data blocking, external search/release authorization, in-flight permission loss, cache exclusion, public-only context, unchanged legacy payloads, and exact paid receipt replay. Model and repository doubles are explicit and are not semantic quality evidence.

Nine affected test files, including the new file, pass 210 tests with four workers. This is a focused compatibility suite, not a full regression run. Full regression, production infrastructure, live GitHub and independent native-model verification are separate pending checks. Prior failures and intermediate test errors are retained beside the candidate; the frozen original source is not edited.
