# Reviewed citation checker evidence, 1.6.6-v4

Status: candidate implementation, not release or a live-model quality result.

## Observed gap and limited repair

The preserved C3 first code-first Tutor block reproduced an independently reviewed
citation exactly, including its source locator. Its independent checker received
the draft and one approved fact summary, without the quote or locator, and returned
` supported=false ` and ` no_new_project_facts=false `. This is an evidence-input
projection gap, not evidence that the Tutor fabricated code. Neither the historical
output nor that negative verdict is changed. A minimal non-Gold fixture preserves
both and their raw artifact hashes in `tests/fixtures/native_checker_evidence`.

The repair gives the checker the complete support and counterevidence of its
current single approved fact. It does not approve the draft, infer new facts from
raw code, relax the semantic decision, or establish learning/mastery/completion.
The second preserved native block removes the same 12 leading ASCII spaces from
each of its six citation lines. It is not byte-exact code. The original quote hash
and display hash remain distinct, and its original negative verdict is unchanged.

An explicit negative semantic result still produces the existing exact-fact
fallback. Missing, changed, revoked, unauthorized or over-budget evidence fails
closed; it never becomes an automatic positive result.

## Admission and compatibility

Only newly admitted explicit Session Tutor turns freeze an internal
`checker_evidence` object containing:

- `contract`: `reviewed-citation-checker-1.6.6-v4`.
- `implementation_hash`: fingerprint of the checker evidence, answer relevance,
  session authority and conversation admission source modules.

The object is bound into the immutable selection snapshot, token, idempotency
receipt, turn and run. It is not accepted from HTTP/model input. Missing from both
historical snapshot and token means the original summary-only checker path.
Removing, changing, or adding it on only one side is invalid. An explicitly marked
run with an unknown implementation cannot silently use a new implementation.

`TurnInput`, `SessionTutorTurnInput`, `RelevantSemanticCheck`, provider response
schema and HTTP routes are unchanged. Unmarked generic turns and old queued Session
turns keep the original system prompt, context keys, dependencies and stage key.
Exact paid receipts remain replayable subject to current authorization. There is
no migration or reinterpretation of old model outputs or the C3 stopped trial.

New checker requests use a versioned stage-key suffix, prompt and evidence context.
The request hash includes exact evidence bytes, locators, session scope and
implementation identity; normal gateway receipt replay also binds dependency
closure, funding and privacy generation. The new identity is included in
`engine_versions()` so existing evolution evaluations cannot approve this code.
No evaluation or release/funding gate has been weakened.

Version 4 also requires the new Session-only funding identity witness described
in `session_funding_witness_v166.md`. Funding identifiers stay in server authority;
the checker sees no new financial data. New in-flight responses require current
active, unexpired funding with the originally admitted payer and namespace. Old
unmarked admissions retain their prior issued-receipt semantics. Earlier marked
candidate implementations are not silently upgraded or replayed by this version.

## Minimum checker input

The existing `question`, `proposed_block` and single `facts` array are retained.
Only the new contract adds `reviewed_evidence`:

- The contract and implementation hash.
- `session_scope`: verified plan/version/module IDs and module content hash,
  publication/unit/source IDs, current single fact ID, activity kind, selection
  manifest ID and snapshot hash. It explicitly means the current block's fact
  within the selected module, not all module facts.
- `fact_binding`: publication/unit/source/fact/claim IDs, separate complete
  `support_refs` and `counterevidence_refs`.
- `citations`: exact canonical quote, repository, commit, path, start/end lines,
  file and quote hashes, read ID, source ID and citation ID for precisely those refs.
- `code_display_contract` and `code_display_checks`: deterministic complete-citation
  display witnesses, binding citation ID, original quote hash, display hash, fixed
  leading-space removal count and `semantic_support_verified=false`.
- A fixed data-use scope limiting citation use to literal source/locator checks
  within the single reviewed fact.

The selected module's server-validated fact membership is sufficient for the
checker to resolve “the current module.” Passing a full plan, module goal, JD,
raw history, unrelated annotations or other module facts is neither necessary nor
authorized by this repair. Source comments and all quoted material are untrusted
data, not instructions. Quote presence may verify a literal code transcription
and its locator; it cannot license new algorithm, runtime, outcome or whole-project
claims. Support and counterevidence keep their distinct meanings.

The only non-byte-exact display relation reported removes the same positive number
of leading ASCII spaces from every nonempty line of the entire citation. Empty
lines remain identical; line order/count, every remaining byte and relative
indentation must match. There is no arbitrary strip, tab removal, newline
normalization, omission, rename, literal change or executable-semantic rewrite.
Only ordinary complete triple-backtick code fences receive this witness. Non-exact
relations are conservatively screened to `.py` citations without visible triple
quotes, backslash-newline continuations or carriage returns. Those cases, and other
source languages, receive no dedent witness; byte-exact matches remain available.
This deliberately conservative screen is not a lexer and cannot establish the
surrounding lexical context of a source fragment. The checker must still determine
that removed bytes are code indentation rather than literal/continuation/template
content. Unknown lexical context or unproven semantics must not be approved from
the relation alone. A witness is never permission to retain a transformation and
never auto-approves semantic support, runtime behavior or the final checker result.

The current upstream review gates quarantine claims with unresolved analyst or
reviewer counterevidence, so reviewer-only counterevidence cannot reach this
accepted r4 checker path. This repair does not bypass that gate or expand the
selection to another reviewer bundle. The projection conservatively preserves
complete claim-linked support/counterevidence refs if supplied by valid authority;
pure synthetic tests of that serialization are not proof that unresolved
counterevidence can pass publication.

## Authority and isolation

Before dispatch, the implementation validates the current accepted plan, active
module, session/turn/run binding and privacy generation. It then reconstructs the
single fact from current publication/review/claim/evidence records under the frozen
selection manifest. Existing r4 standalone review remains mandatory. Exact-source
validation checks the read, quote, file/quote hash and locator; stale caller bytes
cannot become authority. All support and counterevidence refs are kept, without
clipping or guessing links from text overlap.

The checker declares the fresh publication/unit/source/fact/claim/citation roots
and the session selection manifest. Existing gateway role, payer, source scope,
funding, ACL, privacy and dependency checks run before and after the provider call
and during receipt reuse. The service also revalidates the exact evidence after
return. Private-namespace source bytes remain `private_source`, even for public
upstream URLs. No historical ACL or retained fixture provides live permission.

Neither quarantined facts nor a second unit's non-standalone fact becomes an
eligible checker input. No model is called by the fixture or projection tests.

## Budgets, preferences and verification limits

The entire canonical UTF-8 checker context, including draft, quote, provenance,
counterevidence and wrapper overhead, is limited to 100,000 bytes. Existing gateway
250,000-byte total-request limits and run/funding call caps remain unchanged.
Quotes are never truncated to fit. No extra model role or paid call is introduced.

The code-first/no-practice/no-check preferences, independent-mode rules, completion
checks and explicit acknowledgement requirements are unchanged. Structural and
synthetic tests do not demonstrate that a fresh real model will accept a correct
code-first explanation. A new isolated native run and full regression are still
required. Production/Docker/Postgres, real human learning, Gold evaluation and the
missing original six-unit baseline remain separate unverified areas.
