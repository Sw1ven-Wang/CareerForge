# Explicit source paths in 1.6.5

## Defect and original evidence

This repair addresses source-navigation scope selection, not import size limits or repository research completeness.

In 1.6.4, `RetrievalService._symbols` used an explicitly mentioned path only as a ranking boost. Its later branches still admitted exact/fuzzy symbols from other files. `ConversationService._navigate_source` then read the first five candidates. `uncovered_questions` depended on whether any reviewed publication was selected, not whether the requested file was present. The coordinator's `code_focus_ref` was neither a reliable permission grant nor a hard retrieval constraint.

The frozen Pydantic probe explicitly requested `pydantic_ai_slim/pydantic_ai/_agent_graph.py` and asked for a current-snapshot-missing explanation if it had not been imported. The 0033 coordinator correctly returned that path. Nevertheless, nine authenticated blocks from `examples/pydantic_ai_examples/rag.py` and `pydantic_ai_slim/pydantic_ai/run.py` were displayed, no missing-snapshot explanation was given, and `uncovered_questions` was empty. The workflow reported `succeeded`; that did not satisfy the navigation request.

The selected import contains 14 complete files. Its manifest separately records nine excluded files; the requested graph file exceeded the existing complete-file import limit. This does not establish absence from the repository. The old exact `AgentRun.next` request genuinely returned the complete method at lines 635–717 in two pages. That positive observation and the missing-file failure remain separate, unchanged evidence.

Original evidence references and byte verification are in `evidence/explicit_source_paths/ORIGINAL_FAILURE_REFERENCE.json` in the 1.6.5 repair workspace. Their parent final manifest SHA-256 is `350ac6438cd8dc99f0ed1ed7e4522d76d0215440822f970d15d54e6f7d3ecd3d`. The actual selected import JSON SHA-256 is `28909b34a183903bae1cf284c650652f6176296a3ce13abd09f2fb7218d10fba`. No original SQLite connection or original evidence write was used for this diagnosis.

## New, separately frozen contract

New turns freeze `focus.source_path_contract = "explicit-source-path-1.6.5"`. The existing `source-navigation-1.6.3` paging/read contract, `whole-line-pages-1.6.3`, routing schema, quote contracts, build budget contract and database schema are unchanged.

`requested_source_path` derives a bounded navigation constraint from the original user question, before consulting source contents. Model-supplied `code_focus_ref`, query rewrites, cached symbol lists and prior navigation cannot choose or replace that path.

- Direct navigation objects and explicit file/location markers are recognized. Filename extensions are not an allowlist. Bare qualified identifiers such as `Target.run` remain symbol requests, not filenames.
- Standalone quoted path/identifier tokens are distinguished from quoted instructions, code examples and contextual mentions. Comparison/context filenames do not automatically become hard scope.
- Negated navigation, alternatives, multiple path lists, unsupported but explicit path syntax, and unclear attachment require clarification. This includes `show me`, unspaced Chinese alternatives and comma-separated path lists.
- A bare extensionless path can be a repository or a file, so clarification is required; an explicit file/path marker disambiguates it. Unsupported non-ASCII/space-containing path forms tested here also clarify instead of falling back.
- Canonical repository-relative paths are validated using the existing `safe_path` boundary. There is no case folding, basename/suffix substitution, path normalization/repair, URL decoding, filesystem traversal or network fetch. Encoded, absolute, traversal and blocked-directory targets cannot become substitute searches.

This is a deliberately bounded grammar for navigation, not a general natural-language scope solver or new repository discovery feature. Other questions retain the existing intent contracts.

## Scope and permission gates

For a new explicit-path turn, submission freezes `focus.source_path_source_ids` on the server, before any model proposal. The scope comes only from current explicit focus, the existing explicit-switch contract, or an unambiguous literal full repository reference already authorized and visible to the existing coordinator. A path token itself cannot manufacture repository selection. A rejected scope does not turn into the empty-list unrestricted-search sentinel. Ambiguous or missing source selection asks for clarification.

Resolution requires one explicitly selected snapshot. `SourceService.get` validates permission, object state, repository/revision identity, scope hash and actual file manifest before checking the exact path in that snapshot. The lookup does not inspect excluded-file metadata, unauthorized snapshots or nearby filenames, and it exposes no such file lists/counts or hidden source scope. A hidden file in another snapshot cannot affect the result.

An absent target produces:

- `source_path_resolution.status = "missing_from_selected_snapshot"`;
- `requested_source_path_missing_from_snapshot` in `uncovered_questions`;
- zero substitute symbols and zero unrelated reviewed units;
- a server-authored evidence gap explicitly limited to the currently selected authorized imported snapshot;
- no assertion of repository-wide nonexistence, no new raw reads, and no tutor or rewrite call to fill the gap.

A present file with no eligible indexed symbol is `requested_source_symbol_unresolved`, not missing. Existing topic and pinned-publication hard restrictions still apply. A malformed or ambiguous target is a scope clarification, not a claim about file existence.

The path contract/request is part of cache identity. Hard path filtering happens before the candidate limit and model rewrite; fresh selection reconstructs the boundary after cache retrieval. Source resolution is rechecked before output. Snapshot-derived missing-path manifests have blocking source dependencies; displayed missing-path gaps also depend on the source. Permission revocation, privacy changes and source/hash tampering invalidate their replay just as they invalidate positive observations.

Before reading any navigation page, the conversation revalidates the authoritative selection manifest against this turn's question, frozen `source_path_source_ids` and path request. A valid manifest for another authorized snapshot with the same path is rejected, even if it claims the same turn ID/question. It rejects out-of-path cached/staged symbols. Answer commit and replay separately bind the original user target and frozen source ID to the validated read receipt. A genuine current-turn receipt from another otherwise authorized snapshot is not acceptable. Omitting the read contract cannot bypass a new explicit-path constraint.

## Delivery status is not task completion

The global runtime status contract is unchanged. Successfully delivering a gap may still finish a chat workflow normally.

For a funded new explicit-path turn, the saved run result additionally contains `source_path_contract`, `task_satisfied`, `uncovered_questions` and `navigation_outcome`:

- Missing target, clarification, unresolved symbol or exhausted page budget: `task_satisfied = false`, `navigation_outcome = "not_completed"`, with reason codes.
- Raw observations returned without those gaps: `task_satisfied = null`, `navigation_outcome = "source_observations_returned"`. Authentic source spans alone do not certify completion of a descriptive or semantic request.

Unfunded clients inspect the answer blocks, whose new missing/clarification blocks carry `task_satisfied = false`. Consumers must not interpret `turn.status == completed` or `run.status == succeeded` alone as successful source location or semantic completion.

## Compatibility and bounds

Unmarked durable turns pass `source_path_contract=None`, retaining their old request and completed receipt behavior. Old results are not retroactively filtered, reworded or relabelled as passing. New direct retrieval calls default to the new path contract; the direct retrieval request schema itself is unchanged. Historical source/receipt integrity checks remain active.

The existing five-symbol, five-pages-per-symbol and twelve-total-pages limits remain unchanged. Reads are still complete-line pages with persisted receipts and the existing source/quote hashes. Lease, cancellation, policy, privacy and budget checks remain in force. There is no additional model call, dependency installation, upstream code execution or automatic source import.

## Author verification and limits

The new `tests/test_explicit_source_paths_v165.py` contains synthetic offline fixtures, not real model-quality results. The first three tests were run before any product edit and all failed, reproducing absent-path substitution, existing-path spillover and a Chinese snapshot-missing request with later negation. Those original failing outputs are preserved.

The final author counts, exact changed-file hashes and command results are recorded in `evidence/explicit_source_paths/AUTHOR_VALIDATION.json`. Focused coverage includes exact paths, similar/basename/case variants, missing/available-but-unresolved targets, malformed paths, source selection, malicious model routing and cache hints, source hashes, privacy/revocation, cancellation, paging and old completed receipts. Broader production dependencies are not installed. Redis-dependent cases that fail for missing `redis` remain explicit failures, not passes or silently removed tests. This is not a complete production or whole-repository acceptance result.

The author did not run a fresh native model, rerun research, open/copy the original SQLite, build a new control harness or modify the frozen experiment.

## Minimal controlled real retest proposal

The parent controller should independently audit and freeze the final code and protocol before running this separate known-failure retest. Use a new byte-verified copy of the normally terminated Pydantic database; preserve the original database, original results and frozen harness byte for byte. Only that new copy may be opened by the new controller.

1. Use the normal `POST /api/v1/conversations` and `POST /api/v1/conversations/{id}/turns` path, with new conversation/turn identities. The parent's independent preregistration is authoritative: the proposed positive control now explicitly names the existing `pydantic_ai_slim/pydantic_ai/run.py` path and `AgentRun.next`, so it directly tests the new hard-path contract and is not a verbatim rerun of the old path-free exact question. The absent-file question remains the exact original `navigation_absent_file.json.question`. Run each once under separately recorded bounded funding. Freeze those exact strings before execution; do not repair answers, import the excluded file or substitute a test-only answer.
2. Permit at most one fresh `gpt-6.1-sol/high` coordinator request for each navigation case, as separately authorized and preregistered by the parent. Do not call a second worker, repair its output or select a preferred repeat. Stop and report an unexpected additional model request.
3. Observe normal turn events (`GET /api/v1/turns/{id}` or its events endpoint), run result (`GET /api/v1/runs/{id}`), source-read receipts and retrieval manifest. Keep original/new source identities, code hashes, request/response bytes and statuses attributable to the separate retest.
4. Positive control: verify the fixed-commit `AgentRun.next` method still covers lines 635–717 with exact source/page hashes and honest truncation metadata. Do not treat unrelated supplemental symbols as new positive evidence.
5. Missing target: require zero alternate raw-source blocks/reads, no unrelated reviewed units, an explicit current-selected-snapshot gap without global absence claims, nonempty uncovered reason, and funded result `task_satisfied=false`. Check all displayed fields for unauthorized source names/counts/contents.
6. Count both cases even on failure, cancellation, ambiguity or timeout. A successful transport/workflow is not semantic success. This two-case navigation retest neither replaces the original failure nor reruns/re-scores the six-product research experiment.
