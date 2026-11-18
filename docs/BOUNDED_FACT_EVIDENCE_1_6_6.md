# Complete fact/support evidence selection

This document describes the frozen historical r3 executor. New conversation
admissions now freeze r4; see `CONSUMER_CONTEXT_1_6_6.md`. Explicit r3 retains
`question-focused-retrieval-1.6.6-r3` for queued work and typed replay.
Unmarked, original `question-focused-retrieval-1.6.6`, and r2 turns retain their
whole-publication selection. New r3 behavior applies to reviewed knowledge
retrieval, not raw source navigation. It does not approve facts or answers.

## Selection and byte accounting

The service first obtains complete publications through the existing fresh
authorization, dependency closure, immutable version, review, fact, evidence,
source and citation validation. Hard source/topic/pin/path constraints remain
outside ranking. Cache contents are hints, never publication or projected-text
authority. r3 selection does not trust cached candidate order or rewritten
question text.

Fact text and its complete conditions are ranked by deterministic local IDF
over the current question and its deterministic topic aliases. A substantive
question does not inherit a prior topic's aliases. Titles, facets and raw source
quotes cannot outvote reviewed fact text. The strongest matching complete fact
is considered first. Further allocation uses relevance per marginal UTF-8 byte,
including shared canonical support and publication overhead. Fixed identities
break ties. This is a deterministic heuristic, not a semantic relevance proof
or a globally optimal coverage algorithm. An unrelated query can still receive
recall-only candidates; the existing answer relevance checks remain required.

The historically named `max_evidence_tokens` is enforced as the sum of canonical
JSON UTF-8 bytes of returned knowledge units. It is not a tokenizer estimate and
does not cover the enclosing model instructions, user question or other request
fields. The separate model-context limit is unchanged. Budget accounting includes
inline projection metadata. No evidence support or condition is shortened to fit.
The request field now accepts zero bytes. A zero-budget r3 retrieval emits no
reviewed knowledge and no optional rewrite/embedding/reranking call, even when
the run has funding. This does not grant or expand any model-call authorization.

## Lossless projection and provenance

Each selected FactAtom is an exact copy from the validated publication. Its
claim's complete `support_refs` and `counterevidence_refs` are freshly resolved
to canonical citations, including source, commit, path, line span, read ID,
file hash, quote hash and complete quote. Shared citations are deduplicated by
identity only. Facts from different publications or versions are never merged
into a new assertion. Complete publication limitations are preserved.

The immutable selection manifest records `complete-fact-support-1.6.6`, the
ranking/allocation contract, full publication hashes, exact selected fact and
citation IDs, per-fact support/counterevidence bindings, projected hashes, byte
counts and explicit omitted-fact reasons. A single oversize fact is reported as
`single_fact_exceeds_budget`, with its complete byte requirement. Remaining
budget, project-limit and no-current-question-match omissions are separate.
Partially selected publications remain selected; their omitted facts are listed
separately rather than falsely labeling the complete publication absent.

Titles and topic links remain freshly validated publication-level annotations,
not evidence for each selected fact. Facets are included only when all their
referenced claims are selected. The manifest explicitly records metadata scope
and omitted facet IDs. Optional facet summaries are dropped before rejecting a
complete fact/support bundle for size; conditions and citations are never dropped.
Fresh provider reconstruction respects those facet omissions. Complete unmodified publications retain their original
wire shape; their new selection contract is still recorded in the manifest.
The full publication, fact, claim, review, source and read rows are not rewritten.

Before Tutor/Checker use, the hash-checked selection manifest limits allowed identities
and the current publication is validated again. Stale full-publication hashes,
revoked authority, corrupted manifests or facts outside the selected set fail
closed. Cached body text is reconstructed from authority. Per-fact refresh also
removes facets whose other claims are absent. Local projection witnesses are not
duplicated into the merged provider knowledge-unit payload, which cannot exceed
the selected evidence byte cost. The existing unique-FactAtom answer/checker
contract, authorization checks and independent relevance check are unchanged.

## Verification limits

The zero-model public replay uses SQLite backups of the original seven TaskSpecs:
C08-01/02/03 and C08-07/08/09/10. It preserves the hard scope and 12,000-byte budget,
checks exact r2 selection compatibility, and verifies complete selected fact and
support integrity. It also checks the separately published unmapped-key condition
for C08-09 and direct END versus Send(END) boundary for C08-10. This demonstrates
retrieval/support behavior only. It is not a new native Tutor/Checker trial,
answer-quality acceptance, execution of upstream repositories, or independent
model/backend attestation. Production PostgreSQL/pgvector behavior requires its
separate integration environment.
