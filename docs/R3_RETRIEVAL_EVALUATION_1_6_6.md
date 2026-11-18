# Frozen r3 retrieval evaluation

This is an additive evaluator for the current bounded-evidence retriever, not a
claim that current retrieval improves on real independent questions. No real
independent labels were supplied or created for this implementation round. A
passing synthetic fixture is a process test and cannot approve production.

## Explicit admission and compatibility

`POST /api/v1/evolution/r3-retrieval-datasets` and
`careerforge replay r3-retrieval-dataset FILE` accept the separate closed
`RetrievalR3ReplayDatasetInput` schema. The required execution marker is
`bounded-fact-typed-execution-1.6.6-r3-v1`. Its scorer is
`independent-fact-evidence-version-relevance-1.6.6-r3-v1`, matching
`question-focused-retrieval-1.6.6-r3`, `complete-fact-support-1.6.6`, and
`current-question-fact-idf-1.6.6`.

The existing typed dataset API and schema remain historical. Missing markers
still mean `whole-publication-typed-execution-1.6.6`; they never select r3.
Partial, unknown, or cross-target markers fail closed. The old exact-publication
scorer and its original thresholds/assertions are unchanged. In particular,
the old synthetic 0 → 7/9 result is not an r3 result.

## Labels and qualification

Every fact in the explicit frozen publication pool needs an explicit relevant
or irrelevant label. Each label identifies publication, unit version, source,
commit, FactAtom, and the complete support and counterevidence reference lists.
At least one fact must be labeled relevant. Missing candidates cannot silently
become negative examples. Thus this version evaluates answerable labeled
candidate pools, not universal retrieval or no-answer classification.

The dataset also records the independent annotation protocol, external record
reference and SHA-256. These fields bind an annotation record; they do not prove
its truth or fetch it. `human_gold` requires non-synthetic reviewed publications,
at least two authenticated independent annotators, and their individual
approvals of the exact frozen dataset hash. The registrar, executing owner, and
training-feedback authors cannot supply those independent approvals. Human
holdout admission retains the development-calibration, minimum 20 cases and 10
independent-group gates. All existing repository-family, question and time
partition separation checks still apply. Labels, provenance and scorer outputs
are evaluator-only and are never added to retrieval/model or candidate-generation
inputs. This implementation does not invent missing independent labels.

## Execution and measurement

Both arms execute the actual RetrievalService, with its r3 contract explicit.
Both have the same fixed 12,000 canonical UTF-8 knowledge-unit byte budget,
three-project ceiling and authorized per-arm call/request caps. Candidate recipe
parameters remain the object of comparison; evaluation does not tune ordinary
retrieval ranking or increase its evidence budget. Test fixtures can demonstrate
a narrower per-project publication limit improving precision without adding
budget or changing the selector.

A returned unit must be an exact authorized projection of immutable reviewed
facts, conditions, full support/counterevidence, version identity and permitted
metadata. The scorer measures fact precision/recall, support/counterevidence edge
precision/recall, and publication-version precision/recall. A complete wrong fact
from the right publication does not earn fact or evidence relevance credit.
Their mean is the paired score; every component retains the zero-regression gate.
The original paired group-bootstrap, positive-gain, sufficient-data, safety,
latency and cost gates are unchanged. A zero gain remains inconclusive.

## Durable bindings and release

A separate per-arm binding includes the exact executor input hash, label and
annotation provenance hashes, policy hash, engine hash, result hash, full selection
payload hash, run/case/arm identity, and live receipt identities, request/response
hashes, role, profile, status and usage. The actual selection turn identity includes
the run and arm, so reusing another arm's or another run's response/selection fails.
Fresh and resumed stages recheck these bindings. Release reconstructs them from
live authorized objects and receipts rather than trusting a stored passing report.
Revocation, index drift, label changes, missing receipts, or tampered projections
block release; unavailable source-derived trace bodies are not returned.

The replay manifest must also embed exactly the separately frozen dataset's
cases, policy and provenance. Recomputing a corrupted run's self-hash cannot
substitute new labels or thresholds while retaining an old dataset hash.

Promotion and canary start/assignment use the same checked evaluation gate.
Historical evidence cannot certify r3. Fixture-only success is allowed solely as
the existing isolated test-mode process exercise. Human-Gold reports with synthetic
model receipts cannot certify real retrieval quality. Formal success applies only
to the frozen independently labeled candidate pools and measured contract, not to
universal answer quality, learner effectiveness, or runtime execution.

## Verification boundaries

The additive regression suite is `tests/test_r3_retrieval_evaluation_v166.py`.
Original test files remain byte-for-byte unchanged except for the separately
approved additive `tests/support.py` fixture extension for the new answer-completion
schema. No original assertion or budget was relaxed. Reproduction and test/quality logs
are kept under `validation/r3_replay_contract/`; the privileged-storage integrity
reproduction is explicitly synthetic, uses a fresh disposable DB, and reads RC7
source without modifying frozen candidates or accessing native/private evaluation
DBs. Release validation must rerun against an independently frozen source tree.

The annotation `record_sha256` is verified against canonical UTF-8 JSON of
`{"protocol":"complete-fact-evidence-version-relevance-1","cases":[...]}`,
where cases contain every original `RetrievalR3ReplayCase` field in submitted
array order. Canonical JSON uses sorted keys, no extra separators/whitespace,
and unescaped Unicode. Server-added publication/support snapshots are excluded.
This is an exact record-content commitment, separately covered by the two
annotators' approvals. The external record reference is not a claim that the
server fetched or independently authenticated a third-party document.

Human holdout calibration is also rechecked against its live development replay
manifest, immutable dataset, current source authority, engine and receipt records;
a historical scorer or synthetic development report is not a qualifying r3
baseline. Development outcomes remain calibration, never holdout acceptance.

The r3 retrieval cache partition also contains the run and arm identity. A new
replay cannot borrow paid ranking responses from an earlier replay's warmed
cache. Resuming the same admitted stage still reuses its own durable receipts.
The separate original `run.create` idempotency receipt commits the complete
manifest and admitted budget; rewriting run/manifest self-hashes alone cannot
raise either per-arm cap or replace the frozen executor inputs.

Version hits are conjunctive: they count only versions of actually returned facts
that the independent record marks relevant. Returning an irrelevant fact from a
publication/version containing an omitted positive fact receives no version-only
credit. Empty and duplicated selections cannot manufacture improvement. Every
release trace also has to equal its separately saved arm StageArtifact, with the
original frozen input hash, and receipt usage must equal the live receipt record.

The explicit r3 execution contract also names
`frozen-r3-terminal-report-1.6.6-v1`. A committed terminal result and its evaluation
row are saved atomically. Resume and release compare the complete report, including
decision, scores, costs, latency, contract and trace/input identities, with that
original terminal record and its frozen input hash. Rehashing derived timing data
cannot replace an original failed terminal result with a passing report. Missing
or mismatched terminal records fail closed; checks never make model calls.

## Selection authenticity, independently of relevance scoring

Matching hashes across several mutable derived copies is not proof that a
selection was actually produced by the admitted policy. Fresh, resumed and release
validation therefore reconstruct the deterministic bounded selector from the
original authorized publication pool, exact question, hash-pinned taxonomy aliases,
recipe limits and fixed byte budget. Exact selected units, complete support,
normalization and projection must match that reconstruction. It receives no Gold
labels and makes no model calls. A rehashed favorable subset selected under a
different per-project limit cannot manufacture a measured gain.

Under this particular r3 contract, paid embedding/reranker/rewrite outputs do not
drive bounded fact allocation. Their original live receipts establish execution
cost and integrity only. These scores do not measure embedding/reranker quality,
whole-answer correctness or learner benefit. The relevance labels remain separate
from the deterministic selection-authenticity reconstruction.

## Integrity threat model

The storage-integrity probes use a privileged local actor that can corrupt derived
traces, arm stages and evaluation rows in a disposable synthetic DB. They are not
HTTP authorization bypasses. Selection authenticity is reproducible from the
original admitted inputs, so rewriting derived selections and their own hashes
does not establish an actual policy gain.

The original admission receipt, frozen dataset and policy, authorized source
records, genuine model receipts, engine implementation and committed terminal
result remain authority anchors. Hashes and duplicate copies do not protect
against an administrator who can coherently rewrite all of those anchors, nor
do authenticated annotation approvals prove that their human judgments are true.
Those are explicit trust boundaries, not claims of cryptographic storage or
semantic correctness against a fully privileged adversary.
