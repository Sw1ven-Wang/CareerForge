# Reviewed standalone consumer contract

New conversation/retrieval admissions use `question-focused-retrieval-1.6.6-r4`.
They freeze `reviewed-standalone-consumer-1.6.6-v1` and
`complete-standalone-fact-support-1.6.6-v1`. Ranking is unchanged; this is an
eligibility constraint, not a relevance score or new semantic approval.

## Authority and safe fallback

`publication_pack(..., consumer_context_contract=...)` preserves the complete
valid publication, adding an explicit per-published-fact standalone witness. It
revalidates the immutable candidate, original scoped review, coverage, unit/claim/
fact identity, independent read bundle, completed role receipts and request access
witnesses. Both Reviewer and Coverage must positively approve standalone use.
The same global blockers and original evidence conditions continue to apply.
Whole-unit `accept_scoped` alone never implies standalone approval. Unknown or
missing legacy standalone fields become ineligible; unknown contracts fail closed.
The original whole-unit publication remains valid/readable in its original context.

The new atomic consumers omit an ineligible fact with `standalone_review_required`
or reject a requested focus with `FACT_CONTEXT_REQUIRED`. They do not rewrite a
pronoun, guess a dependency graph, borrow a premise from another unit, append raw
source as an approved premise, relax the original review or revive quarantined
facts. A query matching only a contextual fact returns a gap, not an unrelated
standalone fact presented as its replacement.

Budgeting includes the complete context witness, unchanged conditions, full
support/counterevidence and all projection metadata. No byte limit, project cap,
metadata omission or sorting decision can upgrade independence. The original
fact/claim/source/read/review/publication records are not rewritten.

## Consumer coverage

- New retrieval validates the context before bounded selection and freezes it in
  the selection manifest. Old cache bodies cannot supply or change it.
- Tutor and Checker fact refresh derive the same contract from the validated
  manifest, re-read authority and enforce standalone scope before model use.
- New answer commit, streaming re-read, completion refresh and citation navigation
  validate the original r4 selection. A direct membership-only commit cannot add
  the contextual fact back. Source-only navigation remains source-only.
- New personalized, question-observable and deterministic learning proposals use
  this contract. The model text catalog excludes non-standalone facts. Module
  focus selection and fresh module reads check positive standalone review.
- New activities, including activities proposed from old modules, revalidate this
  scope. Their frozen specifications and derived assessment snapshots carry the
  contract. Subsetting a module never supplies missing context.
- New comparisons reconstruct the exact selected facts/facets. They no longer
  replace an r4 projection with an unrestricted complete publication.

Full-unit readers that retain the original reviewed context do not need atomic
approval. This change does not redesign historical reporting, whole-unit review,
source navigation, execution receipt semantics or global promotion gates.

## Historical compatibility

The default unmarked `publication_pack` wire shape is unchanged. Unmarked/r1/r2
whole-unit retrieval and explicitly frozen r3 fact selection remain separate.
The original `complete-fact-support-1.6.6` constant and r3 typed evaluation contract
are unchanged. Existing queued/paid runs, plan inputs, historical modules and
activity specifications are not rewritten or silently assigned a newer contract.
New schemas are not substituted into old paid requests. Historical r3 still has
its original contextual-fact limitation; its replay is not proof of this fix.
New admissions require the new contract. Old artifacts need an explicit new
proposal/activity/retrieval to acquire the new guarantees.

## Evidence and verification boundaries

The native counterexample has FileSearchTool's antecedent in one fact and “该配置”
in another. Its original actual Reviewer and Coverage independently mark the
second fact non-standalone. The new consumer omits it without changing either
fact or verdict. The five retained facts in the first partial publication remain
eligible and its four quarantined facts remain absent.

The portable minimal MIT-source fixture preserves exact original text, conditions,
citations and role verdicts with hashes and license notice. It contains no grants,
actor credentials, internal role orchestration or paid receipt data. Its witness is
mechanical test data, not independent authority. Separate experiment tests use the
real read-only database and actual receipts; they execute the same assertion on
V2 (failure) and this candidate (success), without a model call or database write.
Synthetic service tests cover the operational boundaries and do not establish
native Tutor output, plan quality, task sufficiency, upstream runtime behavior,
production PostgreSQL behavior or complete end-to-end acceptance.
