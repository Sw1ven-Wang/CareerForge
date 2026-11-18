# Explicit public research query contract

This V2 candidate adds the admission-frozen `research_query_contract=public-research-query-1.6.6` alongside, not instead of, the existing `research-planning-context-1.6.6` goal-context contract. Only new admissions opt in. The old `planning_identity()` remains unchanged and continues to identify V1 tasks as V1.

## Observed interface failure

In an isolated V1 native trial, the planner emitted three Boolean-OR query strings. Actual local-first search found the preimported fixed source, while the unchanged GitHub adapter rejected each exact query with `QUERY_SCOPE` before I/O. A separate V1 native reflector also emitted OR-containing queries. The planner/reflector saw the full JD logic but had not been told that requirement OR/NOT semantics and query-string syntax are different contracts.

The original responses, local-first observations, no-I/O adapter rejections and deliberately queued analysis children remain separate retained evidence. They are not rewritten or counted as successful production search. The V1 native trial does not validate the changed V2 prompt or its semantic output.

## New behavior

Both initial planning and reflection receive the same versioned `query_contract` and explicit instructions: existing channels; 2–180 characters; the existing case-insensitive OR/NOT/private/internal query rejection; public-only scope; and external code/documentation authentication. Requirement AND/OR/NOT groups, depth and required/preferred/bonus/not_required status remain in the exact frozen research task. An alternative can be explored as a bounded individual query without converting all alternatives to mandatory requirements; unsearched branches stay unresolved. There are no sample queries, repository names or expected answers in production code.

`validate_public_search_query()` is the single non-I/O grammar predicate, extracted without changing the GitHub adapter's historical length/channel check, rejection regex, error codes or messages. The adapter still enforces authentication, appends its public-scope qualifiers and applies the existing egress/dependency/request budget guards. Authentication is required for external code/documentation search; valid local snapshot search does not acquire GitHub credentials or pretend the external API was tested.

For newly marked research runs, the same validator checks every model-generated query before any branch is added and checks persisted node queries before a local search, cached observation or external fallback can be used. Invalid planner or reflector output fails closed with the original `QUERY`/`QUERY_SCOPE` error. Its paid ModelReceipt and original output remain intact. No query is rewritten, silently split, dropped or retried, and no correction call or new source is invented. If any query in a plan is invalid, that plan does not partially dispatch its other branches. A later explicit retry of a still-active test run reuses the exact paid receipt and repeats the rejection without another model call.

The root/result identity includes the additional query contract only for new marked runs. Unknown query contract versions, incompatible missing planning contracts and tampered manifests fail closed before new dispatch.

## Compatibility

- Original RC11 runs with neither marker retain their original planner/reflector prompt, context, schema, dependency classes and paid wire hashes.
- Existing V1 runs retain the exact goal-context prompt and request shape, without the new grammar instructions or keys. `planning_identity()` never upgrades them to another version. Their historical local-first behavior is preserved; the external adapter still rejects unsafe queries as before.
- Reusing an old idempotency key returns its frozen admission, rather than adding the new marker. Exact paid replay remains unchanged.
- New runs use the extra explicit contract and current task/goal/privacy checks. Query grammar does not grant access, enlarge budgets, satisfy requirements, publish facts or infer learner mastery.
- Direct `source_id`/`repository_url` selection still bypasses discovery. No public API output schema or database migration changes.

## Focused verification

`tests/test_research_query_contract_v166.py` adds 32 deterministic checks for the observed OR failure, unchanged adapter rejection/auth behavior, legal queries, actual local-first lookup, rejected planner/reflection output, exact preserved paid response and no hidden retry, explicit grammar in both roles, unchanged requirement logic, three-version wire/idempotency/replay compatibility, unknown/tampered contracts and resumed invalid nodes.

The existing legacy-simulation helper in `test_research_goal_context_v166.py` removes both new admission fields when constructing an original pre-marker run. That is a fixture correction to represent the historical manifest, not a relaxation of production validation.

Run the new file with the 31 V1 goal-context tests for 63 focused cases. Run the 10 affected files listed in the adjacent candidate report for broader compatibility, followed by the project's Ruff/format and plain `python -m mypy src/careerforge` commands. These are mechanical contract tests with explicit model/repository doubles and no real network. Independent native V2 output, full regression, packaging and production infrastructure remain separate parent-run checks.
