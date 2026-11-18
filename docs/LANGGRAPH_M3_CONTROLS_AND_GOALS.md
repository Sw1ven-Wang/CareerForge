# M3 isolated candidate: controls and command-origin goal versions

This is an engineering candidate, not native-model acceptance or production promotion. The default workflow engine remains `legacy`; the session graph default remains `knowledge-session-v1`. Existing executions retain their pinned engine, graph version, state schema and checkpoint identity. Use `CAREERFORGE_SESSION_GRAPH_VERSION=knowledge-session-v2` only for explicitly opted-in new graph sessions. A genuine command-origin goal amendment explicitly creates a new v2 graph child; it does not migrate its parent.

## API and dialogue

`POST /api/v1/workflows/{id}/pause`, `/resume`, and new `/cancel` use the existing `WorkflowControl` body (`expected_revision`, `idempotency_key`, optional `authorization_ref`). Dialogue `/pause`, `/resume`, `/cancel` follow the same receipt-bound authority. Generic resume and targeted HTTP resume never silently redirect to an ancestor. Explicit original-task language may resolve a paused ancestor before checking its exact control command.

`POST /api/v1/workflows/{id}/goal-amendments` accepts `GoalAmendmentInput`: workflow revision/idempotency key, exact `source_goal_version_id`, `expected_goal_revision`, `topic`, optional `target_depth`, and `learning_priority`. Depth is one of 掌握/熟悉/了解/精通; priority is required/preferred/unspecified. Submitted fields and canonical hash are retained as structured provenance, not fabricated dialogue offsets. Dialogue `增加 LangGraph 技术栈，要求掌握。` preserves the exact original text, UTF-8 hash/byte count and Unicode source spans. Missing qualifiers stay unspecified. Unknown taxonomy remains `pending_mapping` and cannot silently authorize a build.

The older `/goal-revisions` endpoint retains its addendum-only behavior and request identity on legacy run-origin goals. On command-origin goals it explicitly fails `GOAL_REVISION_CONTRACT`; callers must use `/goal-amendments`. Old JD, Analysis, Review and conversation-action-v1 schemas are unchanged. New actions use conversation-action-plan-v2 only where needed. See regenerated `docs/openapi.json` for exact schemas and paths.

## Durable control states

- `active → pause_requested`: transaction records indexed successful action authority, immutable binding, generation and outbox wake, and asks existing Runtime run families to stop cooperatively.
- `pause_requested → paused`: only after an exact persisted graph checkpoint and all affected run families are terminal/paused with no attempted or unknown external receipts. Status does not infer graph pause from child state alone.
- `paused → resume_requested → active`: revalidate original pause certificate, current generation, goal/source/model-profile/funding/deadline and unknown receipts. Worker verifies the saved checkpoint before resuming roots. Resume is never plan acceptance.
- Cancel reuses Runtime fencing/settlement, prevents later paid continuation and never removes completed publications. A completed graph remains completed while controlling its subsequently linked live Tutor roots.

`workflow_control_commands` is unique by workflow/generation and receipt/action index. Duplicate delivery cannot create a new command or side effect. Existing `workflow.execute` outbox and wake generations remain the delivery mechanism. No DB ownership transaction spans saver network I/O.

New v2 graph topology adds a real control interrupt. Existing v1 graph topology, state keys and execution implementation are preserved, with an external compatibility adapter observing the original checkpoint. For historical v1 pause-before-first-invocation, the adapter safely persists original START while the control fence prevents work, without inventing an old receipt. Terminal completion races reconcile the already-persisted completed checkpoint instead of failing an accepted execution.

For migrated completed rows with no new terminal witness, a requested pause schedules a read-only observation of the original saved terminal checkpoint. Exact identity, successful indexed acceptance and current command are rechecked before recording that witness. The completed graph is never reopened. Saver transport failure leaves a requested command and recoverable wake; absent/invalid historical checkpoint blocks pause acknowledgement rather than inventing history. Cancellation is still permitted and does not require saver availability.

## Immutable GoalVersion and downstream scope

Migrations add discriminated origin to GoalVersion. `run-goal-v1` retains a non-null real run. `command-goal-v1` requires null run, parent goal, matching goal-addendum-v2, version hash and successful indexed action receipt. Creation itself does not create a Run, model receipt, funding or employer requirement. Old object bytes, rawJD envelope, offsets and employer requirements remain intact; user objectives are separate, explicitly sourced and never claim user mastery.

The server freezes `goal-build-scope-v1` with the non-null new GoalVersion, exact object/version hashes, selected user objective, verified taxonomy, optional employer subset, exact question/dimensions and source/commit restrictions. Empty employer subset is allowed only in this validated user-objective mode. Scope propagates through build, research, Analysis and Review roots/children; paid leases revalidate bindings even if markers were removed. Replays check admitted identity before selecting reused material or creating roots.

Reused reviewed material does not create a review/publication and does not prove whole-goal coverage, requested depth or competence. Status exposes conservative `user_objective_coverage` separately. The new `learning-goal-amendment-plan-v1` and `learning-text-catalog-goal-amendment-v1` validate exact scope and preserve the full old JD envelope plus distinct user objectives. User-objective catalog entries are not evidence. Acceptance and Tutor authority bind the exact new goal, proposal and plan version; old acceptance cannot authorize a new plan.

## Migration, privacy and rollback

Upgrade chain: `cf166_session_graph → cf166_goal_amendment → cf166_workflow_controls`. Existing rows default to run-goal-v1, session-v1 and control generation zero. No historical receipt/checkpoint is backfilled. Structural privacy erasure preserves safe discriminators while rejecting commands, clearing checkpoint references and fencing execution as required.

Rollback means disable new admission, retain schema/checkpoints and run the appropriate versioned readers/workers until old work is settled. Downgrade refuses retained command-origin goal history or graph/control history before destructive DDL. Never delete history to force rollback, rewrite graph versions, resume a cancelled run, or switch the global default as part of M3.

## Validation boundary

Focused synthetic tests cover command provenance/CAS/replay, immutable JD, scope tampering, old routes, exact plan approval, repeated control cycles, saver outage, completion races, historical v1 checkpoints, live Tutor roots, cancellation and privacy. Opt-in production-marked tests add PostgreSQL command concurrency/constraints and real saver restart. They are not automatically evidence of a full PostgreSQL-domain/Redis/Celery crash gate or real Pi/Kama acceptance. Candidate release report records completed and still-pending gates separately.

## Reviewed context-contract integration

This candidate also carries the separately reviewed opt-in `context-once-v1` transport contract. It keeps the full original generated context once in the unchanged user message with an exact hash/byte-length binding. It does not truncate evidence, change Research/Analysis/Review schemas, raise the 250,000-byte cap, or authorize spending. Legacy remains the default; new-contract runs pin a distinct capable provider profile, preserve complete authority fields and reject cross-profile replay/unknown outcomes.

The planner's business-context threshold still sizes the expanded historical representation, including the M3 goal/catalog payload, so transport deduplication does not silently choose a different planner prompt. M3 scope, JD lineage and exact plan approval remain separate authorities. Synthetic integration tests cover packed scoped build → Analysis → Review → new plan, no-approval pause/resume, explicit exact-version confirmation/Tutor, and precharge envelope/profile/scope/source guards. Offline/native-helper commands for this merged source are in `../release/context_validation_scripts/M3_HANDOFF.md`; copied context-candidate commands are archival, not the merged test path. Full merged aggregate/persistence and native acceptance remain separately gated.
