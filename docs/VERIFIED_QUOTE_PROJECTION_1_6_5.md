# Verified evidence quotations in learning projections (1.6.5)

## Scope and evidence

This repair changes the learning presentation path only. It does not change a
model answer, source research, evidence adjudication, source execution, publication
rules, plan acceptance, or learning-quality assessment.

The frozen 1.6.4 Smol study reported `PLAN_TEXT_SCOPE`, with no resulting plan,
plan version, or plan module. The original runtime log did not name the failing
field. Code-order inspection identified
`uncovered_goal_requirements[7].evidence[0]`, a verbatim `fact_text` catalog
quotation describing a local executor's lack of a security boundary. The
presentation regex matched the passive warning `绕过，不能作为安全…` as though it
were an instruction to bypass security. Gap projection runs before modules are
persisted, and the enclosing transaction rolls back the failed proposal.

A new read-only JSON characterization independently reproduces that rejection
with the unmarked policy. It does not open the original SQLite database. Applying
the new server policy to an in-memory copy of the frozen input, with the recorded
model output unchanged, permits projection of all nine gaps and six module tasks.
This is a deterministic counterfactual projection, **not** a new native-model run,
persisted successful plan, or revised result for the original study.

The original 1.6.4/1.6.3/1.6.2 files, databases, results and traces remain immutable.
Of the six primary cases, the four normally completed research roots remain
partial. Mini and Llama were interrupted by infrastructure failures; their
original product runs were nonterminal and their quality was `not_assessable`.
This patch cannot upgrade any of those outcomes.

## Policy boundary

New `start_personalized_plan` requests freeze
`quote_projection_version = verified-evidence-quote-1.6.5` on the server. The
public request, planner output, and text-reference schemas do not accept this
field or a model-declared trust flag. Unknown policy values fail closed before a
model call. No schema, existing rule text, text-catalog version, build contract,
or quote-correction contract is changed.

`resolve_reference` still requires an exact substring of the selected authorized
catalog entry. It checks the permitted origin, publication, and focused facts
before applying the presentation policy. There is no lookup of arbitrary source
IDs and no normalization of the quoted value for membership testing.

Only source evidence origins (`fact_text`, `fact_condition`, `unit_limitation`)
may retain a grammatically passive or negative safety warning. The exception is
not keyed to Smol, a source ID, an answer, or a particular quoted sentence. A
source excerpt remains data, not trusted instructions, an independent factual
claim, proof of coverage, a runtime result, or evidence of learner competence.

The old free-text/display restrictions are retained for goals, titles from the
request, and self-reported context. Legacy free-form missing-goal prose is still
never echoed; it can only select a safe matching goal excerpt. All steps,
scenarios, checks, reason codes and activity kinds remain closed operations.

The new policy also checks:

- Both the selected excerpt and its whole catalog entry, so selecting a token
  suffix cannot remove its credential or instruction context.
- Control/format/surrogate characters, active role/markup cues, instruction
  overrides, exfiltration cues, and common secret-like keys, tokens and values.
- Bounded NFKC, percent, HTML-entity, Unicode/hex-escape and base64 inspection,
  including common nested forms. Detection does not decode the displayed quote.
- Punctuation-separated instructions. Every safety-action occurrence is checked,
  including actions inside an earlier broad regex match. A negative/passive
  prefix does not bless subsequent commands. Prefix checks retain actual
  punctuation boundaries rather than borrowing a negation from a prior sentence.

These are conservative, bounded presentation checks, not a complete semantic
prompt-injection detector or a universal secret classifier. They can reject
otherwise useful source excerpts, including safe slices of an unsafe full entry.
Arbitrary encodings and every possible natural-language attack are not claimed
to be recognized. The durable safety boundary remains source/namespace/goal
authorization, exact references, closed server operations, and separation of data
from instructions. The model is never granted authority by passing this filter.

## Consumers and data/instruction separation

The learning planner continues to receive unchanged server `PLAN_RULES_V162` as
its system message. The model gateway serializes the frozen catalog and other
context under `UNTRUSTED_TASK_DATA` in the user data message. Quoted material is
never installed as a system/developer message or tool definition.

For new projections, source-derived titles, scenario descriptions, acceptance
criteria, gap summaries and activity prompts use `display_reference`. It labels
the excerpt as data and uses JSON string quoting, preserving the exact `quote`
field while escaping embedded quotes and newlines in combined presentation text.
Exception-sequence targets still must be a single bounded symbol; their values
cannot introduce arbitrary instructions. Structured references retain provenance
and `quoted_data_not_instruction`; that boolean is output by the server, not
accepted as authority from the model.

The HTTP routes return JSON. The CLI uses `Console.print_json`; the application
has no HTML renderer that interprets these strings as markup. Activity prompt and
rubric strings are frozen presentation for the learner, not model system prompts.
The coding path obtains currently authorized publication packs and a separately
requested question, not the activity text as system instructions. It keeps fixed
coding rules, an untrusted-data message, restricted tools, and independent command
approval. This patch neither runs nor recommends executing upstream code.

## Authorization, lifecycle and replay

No change is made to:

- Current publication-pack validation, dependency closure, source funding scope,
  private-data classes, owner/namespace access, or registered-goal validation.
- Revalidation before model dispatch, after the response and before commit, or on
  subsequent plan/detail/activity reads. Revocation or privacy erasure is still a
  fence, not overridden by a frozen catalog or quote-policy marker.
- One-call plan budgets, receipt hashing, cancellation/lease fences, idempotency,
  all-or-nothing proposal transactions, opt-outs or independent attempts.
- Separate plan acceptance, runtime approval, or publication authorization.

Missing policy markers retain the old filter and old rendering byte-for-byte.
Historical successful projections can still validate their immutable hashes.
Historical failed requests are not silently upgraded or retried. The new marker
changes a **new request's** frozen input and therefore its request hash; a stored
old receipt must never be relabelled as a new request. Tests include an unmarked
1.6.4-style paid-receipt interruption/replay in a fresh synthetic database and
verify that replay changes neither input nor receipt and consumes no second call.

## Verification and limits

New tests live in `tests/test_verified_quote_projection_v165.py`. They use unrelated
synthetic warning text and fresh pytest databases; none imports a historical model
answer or sealed upstream repository. The negative matrix covers direct, encoded,
punctuated and role-spoofing injection; secret-like whole-entry/suffix selection;
tampered or normalized/nonverbatim references; wrong origin/publication/focus;
unselected sources; fake trust fields; revoked/private sources; and ordinary goal
fields. Service checks cover rollback, one-call/idempotency, data-message routing,
activity rendering, and exact unmarked receipt replay. Existing adversarial tests
are not weakened or edited.

Actual commands, test output, static-check output, a reproducible JSON-only
characterization script and the exact diff are saved separately under the new
repair evidence directory, `evidence/verified_quote_projection`. The initial new
test pass was 127/127. The final check summary records the expanded final test
counts and any failures; it is not a production/deployment certificate.

No full application suite, production PostgreSQL/Redis/Docker validation, paid API,
native bridge, fresh model evaluation, upstream repository execution, installation,
or external publication is performed as part of this repair.

## Controlled retest proposal

1. Independently audit the exact patch and final focused-check logs, then freeze a
   new source/evidence manifest. Keep the original study manifest unchanged.
2. For deterministic characterization, run the supplied `reproject_original_json.py`
   with the existing Python environment and the 1.6.5 source path. It reads only
   the original request and raw-output JSON, verifies their before/after SHA-256
   values, changes only the in-memory policy marker, and writes explicitly labelled
   counterfactual evidence into the new repair directory. It does not create a
   plan, touch SQLite or count as native validation.
3. The proposed real retest uses a distinct directory and a byte-for-byte copy of
   the frozen Smol terminal database. Verify copy hashes before opening **only the
   copy** with the application. Never open the original with SQLite. Temporary
   synthetic API credentials, if required, belong only in the copy; record their
   exact row changes separately from the test's row diff. Keep source, publication,
   goal, profile preferences and original failure rows unchanged. An empty database
   populated with synthetic fixtures remains suitable for deterministic service
   checks, as already demonstrated by the focused tests.
4. Only after the audit/freeze and separate parent authorization, create a fresh
   sol/high native request with new run/receipt IDs, the same explicitly authorized
   source scope and current policy marker. Retain the normal single-call budget,
   cancellations, output validation and no-implicit-retry behavior. Keep raw output
   immutable and separate from server projections.
5. Use the ordinary `POST /api/v1/funding` endpoint with `call_cap=1`, the sole
   `learning_planner` role, the original data classes and source allowlist, then
   `POST /api/v1/learning/plan-proposals` with the original publication selection,
   title, learning intent, unchanged profile revision and a new idempotency key.
   Read the result through `GET /api/v1/runs/{id}` and `GET /api/v1/plans/{id}`.
   Do not accept the plan, create an activity, or start an attempt.
6. Verify one new run and one fresh model receipt, one consumed call and zero held
   calls at termination. On success, verify one new `LearningPlan`, one
   `PlanVersion`, one `PlanProposalManifest`, and exactly the module count in the
   **fresh** valid draft, with matching module/manifest hashes and a persisted
   result stage. The plan must remain revision 1, `active_version_id=null`, and
   `accepted=false`; the learning profile's active selection must remain unchanged.
   All displayed references must still match the current selected catalog. The
   fresh output is not required or forced to copy the old nine-gap/six-module
   structure. On failure, preserve the failure and receipt, with no partial plan,
   version, manifest or modules. Record a full before/after row diff in the copy,
   accounting for new audit/dependency/idempotency rows and any legitimate counter
   changes; prove the old failed run and original source/goal/publication rows were
   not rewritten. Report new outcomes separately. A projection pass alone does not
   establish complete JD coverage, repository-structure understanding,
   technical-system quality, runtime security, or effective learning.
