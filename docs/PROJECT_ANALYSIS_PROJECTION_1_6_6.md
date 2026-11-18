# Reviewed project-analysis projection (C05)

Contract: `reviewed-project-analysis-1.6.6`.

This is a deterministic, authenticated presentation of existing source snapshots,
read receipts, publications and independent task-coverage receipts. It makes **no
model calls, new knowledge facts, new source-read receipts or runtime observations**.
It does not classify prose by keywords, infer module roles, extract graph endpoints,
or turn symbol/file counts into semantic coverage.

## Entry points and replay compatibility

- New builds receive the server-controlled `project_analysis_contract_version`
  admission marker. Their saved result and `GET /api/v1/runs/{run_id}` result expose
  the eight C05 fields below, while preserving all previous result fields.
- `GET /api/v1/knowledge/builds/{run_id}/project-analysis` explicitly requests the
  projection for an owned build, including a historical build. It never updates
  the old manifest, result, receipts, source metadata or publications.
- CLI: `careerforge project-analysis RUN_ID`. This issues the authenticated GET
  only. IDs are one URL segment, not URLs, query strings or filesystem paths.
- Unmarked queued builds still use their original model prompts, schemas,
  allocations, execution and result shape. Merely reading an old build with the
  ordinary runs endpoint does not silently upgrade it.
- No result yet: HTTP 409 / `PROJECT_ANALYSIS_PENDING`. Wrong owner: 404. Missing
  login: 401. Revoked evidence, damaged source/claim/citation or mismatched
  versions fail closed with the existing integrity/access error or
  `PROJECT_ANALYSIS_BINDING`. Unknown future projection versions return 409.

All views are build-owner-only, including for public source/publication data.
Source namespace grants do not grant access to a private JD or its coverage.
The projection does not write private JD content into a public knowledge unit.

## Schema

The Pydantic envelope is in `contracts_project_analysis_v166.py`; its generated
JSON Schema is `docs/schemas/project_analysis_1_6_6.schema.json`. The eight public
C05 names are required. Existing publication-pack and research-requirement
subdocuments retain their established shapes rather than inventing replacements.
The detailed dictionaries in that envelope have the following versioned shape.

### Envelope and provenance

- `project_analysis_contract_version`: the exact contract string above.
- `projection`: `run_id`, `run_input_hash`, nullable `admission_contract`,
  `read_only=true`, `additional_model_calls=0`,
  `model_fact_transformation="exact_approved_text_and_conditions"`,
  `scope="imported_snapshot_only"`, `full_repository_understanding=false`,
  `runtime_execution_observed=false`, nullable `goal_version_id`,
  `other_goals_status="not_evaluated_requires_separate_goal_build"`,
  `publication_ids`, nullable `independent_coverage_receipt`, `reason_codes`,
  and `content_hash` (the canonical fingerprint of the complete envelope before
  adding this hash itself).
- A coverage receipt binding contains `receipt_id`, `run_id`,
  `stage_key="task.sufficiency"`, `request_hash`, `response_hash` and
  `stage_payload_hash`. This is recorded logical-role provenance, not independent
  attestation of a provider's backend model identity.

### Exact reviewed-claim record

Every semantic section item has:

- `source_id`, `repository`, nullable `commit`, `unit_version_id`,
  `unit_content_hash`, `unit_render_hash`, `publication_id`, `review_id`,
  `subject_key`, `claim_key`, `claim_id`, `claim_hash`, `fact_atom_id`.
- `statement`, `conditions` and `kind`, taken exactly from the approved claim
  and hashed original unit draft. No summarization, qualifier removal, endpoint
  extraction, or new semantic assertion occurs.
- `citations` from that claim's analysis support references and
  `independent_citations` from its accepted review decision. Each citation retains
  `citation_id`, `source_id`, `repository`, `commit`, `path`, `start_line`,
  `end_line`, `quote`, `file_hash`, `quote_hash`, and `read_id`.

Both sets of reads are checked against their actual fixed source text and the
correct role/run/principal. An analysis read cannot replace an independent
reviewer's read. The published unit's analysis and review runs must be this
build's admitted children. A different commit's publication cannot be substituted,
even if its repository, subject or claim key is the same.

### `repo_map`

Object with `observation_kind="static_index_not_semantic_coverage"`, `sources`,
`reviewed_structure`, `unread_scopes`, `cross_source_composition=false`.

Each source has identity (`source_id`, `repository`, `commit`, `scope_hash`,
`origin_kind`, `license_id`), `observation_kind`, `file_count`, `files`,
`entry_candidates`, `entry_candidates_are_verified_entrypoints=false`,
`verified_reads`, `outside_imported_snapshot="unknown_not_enumerated"`,
`full_repository_read=false`, and `executed=false`.

Each file has the existing static index fields `path`, `language`, `parser`,
`parse_status`, `symbols`, `lines`, `file_hash`, plus `verified_read_ids` and
`unread_line_ranges={analysis: [...], reviewer: [...]}`. Each range is inclusive
`{start_line,end_line}`. These are complements of frozen, verified read windows,
not percentages of semantic understanding. Unimported repository files are not
enumerated or claimed read.

Each verified read has `read_id`, `source_id`, `path`, `start_line`, `end_line`,
`file_hash`, `quote_hash`, `commit`, `role`, `run_id`, `bundle_stage_key`, and
`bundle_hash`. Only already committed analysis/reviewer read bundles count. The
static-index computation creates no read receipt. The index is recomputed from
validated imported bytes; mutable cached maps/symbol rows cannot supply facts.

### `modules`, `call_chain_edges`, `exception_boundaries`

Each is a `ReviewedStructureSection`:

- `dimension`: respectively `module_responsibilities`, `call_flow`, `boundaries`.
  `repo_map.reviewed_structure` uses `project_structure`.
- `status`: `known`, `partial` or `unknown`, within the explicit imported scope.
- `representation="scope_bound_reviewed_claim_narratives"`, `items` containing
  exact reviewed-claim records, `scoped_assessments`, and `reason_codes`.
- `typed_edges_available=false`, `full_repository_coverage=false`.

`call_chain_edges` is a compatibility name. Its items are exact call-flow
narratives selected by the independent reviewer's own call-flow anchors, **not**
invented `from`/`to` graph edges. Likewise, `exception_boundaries` preserves the
reviewed `boundaries` narratives; it does not infer an exhaustive exception
taxonomy or attach an exception type to every generic boundary statement.

Each scoped assessment preserves `source_id`, exact `unit_version_ids`,
`reported_status`, effective `status`, `reviewer_explanation`,
`unresolved_questions`, resolved `claim_ids`, `reason_codes`, and its
`assessment_receipt`. The explanation is labelled assessment metadata, not a new
approved source fact.

Only actual corresponding dimension anchors from the committed, completed
independent composition-review receipt are used. The receipt must reproduce the
stored coverage stage, and its server-added unit set must equal this source's
exact published set. Analysis self-assessment, a topic label, fact acceptance,
or a title alone cannot fill a dimension. Missing composition review or anchors
returns explicit unknown with reasons, not a failed ordinary fixture build.
Rejected, unpublished, wrong-source, wrong-version and ambiguous claim references
do not appear as facts. Valid remaining anchors may be shown as partial; missing
anchors are never silently declared complete. Known requires valid known
assessments without unresolved dimension questions for every scoped source.

### `key_symbols`

Object with `observation_kind="static_index_not_semantic_coverage"`,
`selection="indexed_symbols_overlapping_verified_read_windows"`,
`index_symbol_count`, `selected_symbol_count`, `items`,
`read_overlap_is_not_full_symbol_read=true`, and
`symbol_presence_is_not_implementation_coverage=true`.

Each item is a static indexed declaration with `source_id`, `path`, `name`,
`kind`, `start_line`, `end_line`, `parser`, `repository`, `commit`, `file_hash`,
`overlapping_read_ids`, and `semantic_importance_assessed=false`. The deterministic
selection is line overlap, not a claim that the declaration is important or that
its whole body was read. Empty selection is permitted when no verified reads
overlap an indexed declaration. Text-only/parse-failed files remain explicit in
the inventory and do not acquire invented symbols.

### `knowledge_units`

List of existing publication-pack fields plus `reviewed_claims`, containing
the exact records described above. Original `facts`, `citations`, limitations,
topic/facet associations, synthetic flags and `runtime_observed=false` remain.
Facts follow the frozen render order; otherwise-unordered citations, topic links
and facets use stable IDs rather than database scan order. No original knowledge
fact is created or modified by this projection.

### `jd_mechanism_evidence_links`

Object with nullable `goal_version_id`,
`coverage_scope="single_frozen_goal_only"`,
`other_goals_status="not_evaluated_requires_separate_goal_build"`,
`requirements`, `unresolved_requirement_ids`, `user_mastery_unchanged=true`.

Each requirement record preserves the complete frozen `requirement` (including
its JD identity, original quote/offset and qualifiers when available),
`evidence_link_status`, exact `evidence`, `scoped_assessments`, `reason_codes`,
and independent `dimensions` for that requirement. It also sets
`target_depth_satisfaction_claimed=false`,
`evidence_link_is_not_depth_sufficiency=true`, `user_mastery_unchanged=true`.
A known link is not proof of target depth, overall task completion or user
mastery. The existing `task_coverage` result remains a separate contract.

A single run covers only its actual frozen goal version. For the public cases
requesting JD-A **and** JD-B, projecting a run for either goal does not cover the
other. The other goal explicitly remains unassessed and needs its own authorized
build. No hard-coded case identity or evaluator data enters the implementation.

### `open_questions`

Deduplicated exact question strings from the saved unanswered questions and
independent coverage assessments, including nested requirement dimensions.
Missing-structure reason codes and unread spans remain separately visible even
when no reviewer wrote a question string. Empty questions do not certify
completion.

## Reproducible zero-call replay and verification

Run from the project using its existing Python environment:

```sh
PYTHONPATH=src ../.venv/bin/python scripts/replay_project_analysis.py \
  --evidence-dir ../evidence/native_candidate_openai \
  --output-dir validation/project_analysis/openai_NEW
PYTHONPATH=src ../.venv/bin/python scripts/replay_project_analysis.py \
  --evidence-dir ../evidence/native_candidate_langgraph_r2 \
  --output-dir validation/project_analysis/langgraph_NEW
```

Use fresh output directories. The script copies SQLite and sidecars without
opening the originals, creates a consistent backup of those copies, and reads
only that backup. It records unchanged original file hashes, manifest/result
hashes, model receipt counts, source-read counts and consumed calls. It writes
`before.json`, `after.json`, and `report.json`; repeated projection equality is
checked. This is deterministic replay of existing model evidence, not a new
native experiment or independent semantic acceptance result.

Focused tests: `tests/test_project_analysis_projection_v166.py`, plus existing
source-parser, research-coverage, build-execution and API integration tests.
They cover known/partial/unknown, absent independent anchors, unpublished or
rejected references, cross-version grafts, revoked/stale evidence, static index
versus reads, exact text preservation, no added calls, owner-only API, CLI,
new admissions and historical queued-result compatibility. They are mechanical
fixtures, not a model-quality score. New native C04/C05 trials remain separate.
