# Amendment 002: require explicit target context for ambiguous identifiers

## Final new-turn contract

This amendment supersedes the candidate002 intermediate exception that allowed a recognized navigation verb to establish symbol type. `Locate` does not determine whether `missing.py` or `AgentRun.next` is a filename or qualified symbol. New guarded LOCATE requests with bare dotted candidates and no explicit file/symbol context therefore clarify, for any verb. No extension allowlist, capitalization heuristic, available-file test or approximate-symbol match chooses the target type.

Use `Locate symbol Target.run` (or explicit function/method/符号/函数/方法 context) for symbol navigation. A relative slash target without an extension is still a path-like candidate and cannot become an unrestricted lookup merely because it is unsupported. A slash token is a repository scope only when explicitly labeled repository/repo/project/仓库/项目, not because it follows a navigation verb or precedes 的 + symbol. Unknown or unclear candidates ask for clarification with `task_satisfied=false`, without a file-missing claim. Non-LOCATE discussion, quoted instructions, code examples and recognized contextual mentions remain data.

This is a deliberate user-visible clarification contract, not a general natural-language classifier. It does not expand source access, import sources, execute code, spend a model call or change global workflow status. The old positive path-parser function itself is unchanged. New exact-path requests retain frozen source/path binding and all original permission, hash, paging, replay, cancellation and budget checks.

## Durable compatibility

Only new server-marked turns activate `locate-path-guard-1.6.5.2`. Old turns without the marker, including candidate001 turns carrying the original path contract, retain their original question and receipt semantics. No immutable completed receipt is rewritten or reclassified. The new author tests pair original bare-qualified and implicit-repository questions with (a) new guarded clarification and zero reads and (b) original unguarded real navigation followed by unchanged completed replay. They separately prove explicit symbol and labeled repository requests still read actual source.

## Authorized migration of positive test inputs

The final contract conflicts with tests that intentionally test paging, cancellation, receipt integrity or ACLs using ambiguous shorthand as their setup. This amendment adopts the smallest input migration, independently reviewed in `PROPOSED_TEST_INPUT_MIGRATION.patch` before application.

Only four input literals in two existing candidate002 files change:

- `tests/test_source_navigation_v163.py`: the shared `submit_navigation` default and two direct RetrievalTaskSpec questions change `Locate Target.run` to `Locate symbol Target.run`.
- `tests/test_retrieval_guard.py`: the repository-scope positive changes `定位 fixtures/agent 的 echo 函数` to `定位 仓库 fixtures/agent 的 echo 函数`.

All existing output, permission, source, hashing, pagination, budget, cancellation, replay and no-model-call assertions are preserved. No test function, parameterized case, assertion, skip or xfail is removed or weakened. The shared helper is imported by other files, so the list below is 12 **actually observed failing cases before migration**, not a claim that it has only 12 callers. Candidate001 files are unchanged. The original ambiguous input is retained in new guarded-negative and unguarded-legacy controls; migration does not hide the behavior change.

### Actually observed affected cases

- `tests/test_source_navigation_v163.py::test_exact_qualified_target_ranks_before_limit_even_at_original_rank_eleven[9]`
- `tests/test_source_navigation_v163.py::test_exact_qualified_target_ranks_before_limit_even_at_original_rank_eleven[35]`
- `tests/test_source_navigation_v163.py::test_character_pages_are_exact_and_long_nearby_symbol_does_not_suppress_target`
- `tests/test_source_navigation_v163.py::test_failed_nearby_read_preserves_pages_and_reports_remaining_range`
- `tests/test_source_navigation_v163.py::test_page_budget_is_explicit_and_returns_cursor_not_fake_completion`
- `tests/test_source_navigation_v163.py::test_new_pages_replay_idempotently_after_later_read_interruption`
- `tests/test_source_navigation_v163.py::test_cancel_after_first_page_stops_next_read_without_erasing_first`
- `tests/test_source_navigation_v163.py::test_historical_navigation_rechecks_source_and_receipt_hashes[source]`
- `tests/test_source_navigation_v163.py::test_historical_navigation_rechecks_source_and_receipt_hashes[receipt]`
- `tests/test_source_navigation_v163.py::test_source_acl_is_not_bypassed_by_exact_name_or_saved_navigation`
- `tests/test_source_navigation_v163.py::test_unmarked_legacy_navigation_keeps_original_read_contract`
- `tests/test_retrieval_guard.py::test_symbol_lookup_skips_model_and_preserves_followup_focus`

## Evidence chronology and final gate

All evidence is under `candidate_002/evidence/path_guard` unless otherwise stated.

- `01_before_guard_four_cases.txt`: original four service probes, 3 failed / 1 passed, before candidate002 product edits.
- `08_focused_regressions.txt`: intermediate 228 passes, before the bare/extensionless amendment.
- `09_before_bare_extensionless_guard.txt`: two real substitute-read failures, retained.
- `11_final_focused_regressions.txt`: intermediate 234 passes before the final bare/implicit-repository contract; the filename is historical and does not designate final readiness.
- Independent `bare_filename_probe`, `extensionless_probe` and `adjacent_initial.log` remain preserved under `candidate_002/evidence/independent_audit`.
- `14_before_symbol_fixture_migration.txt`: 221 passed / 13 failed with the new guard and old inputs. Eleven failures were existing symbol-navigation cases; two were intermediate new-author expectations revised to the final contract.
- `15_before_repository_fixture_migration.txt`: the one repository-scope positive failed before its explicit-label migration.
- `TEST_INPUT_MIGRATION_PROPOSAL.json` and `PROPOSED_TEST_INPUT_MIGRATION.patch`: exact measured IDs and pre-reviewed four-literal change.
- `AUTHOR_VALIDATION.json`, `AUTHOR_CODE_FREEZE_002.json` and `candidate002_path_guard_final.patch`: final author outcome, complete hashes, preservation checks and diff. Earlier provisional marker/patch files are preserved, not overwritten.

Full offline testing and fresh real-model retesting are separate release gates. No historical/native result is replaced by synthetic tests. No original SQLite is opened or copied by this author, and no source execution, installation, networking or model dispatch is performed here.
