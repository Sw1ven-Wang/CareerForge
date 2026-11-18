# 1.6.6 CLI integration

These opt-in commands connect the repaired contracts to the ordinary terminal
client. Existing `build`, `chat`, `sources`, and learning proposal/acceptance
commands keep their original behavior. The CLI does not grant funding, publish
knowledge, run repository code, mark mastery, or manufacture a human review.

Use actual IDs returned by your own instance in place of the placeholders below.
JSON files contain explicit scope, funding and idempotency keys; missing or
malformed inputs fail before a request. Error diagnostics do not echo private
file contents. Start the normal API and worker, then use the installed
`careerforge` entry point. From a source checkout, set `PYTHONPATH=src` when
running `python -m careerforge.cli.main`.

## Literal JD import and separately budgeted extraction

```sh
careerforge jd import ./jd.txt --namespace PRIVATE_NAMESPACE --jd-id MY_JD_ID
careerforge jd show JD_REVISION_ID
careerforge jd extract ./extract.json
careerforge tasks --run EXTRACTION_RUN_ID
```

Import reads the user-selected UTF-8 file as bytes, preserving CRLF, whitespace,
Unicode and BOM. It supplies the SHA-256 of those exact bytes and their byte
count. The 50,000-character server limit and a 200,000-byte client bound apply.
There is no text rewriting or guessed job title/source. Unspecified `job_title`,
`source`, `source_version`, and `paragraphs` remain null. Backend paragraph
segmentation remains derived from the original document.

Optional metadata flags are `--job-title`, `--source-kind`, `--source-label`,
`--source-url`, and `--source-version`. Only explicitly supplied fields are set.
If just one source field is supplied, other fields in that source object remain
null. `--source-url` is stored provenance, not permission to fetch that URL. No
source URL or path is executed or interpolated into a shell command.

`extract.json`:

```json
{
  "namespace_id": "PRIVATE_NAMESPACE",
  "funding_id": "ALREADY_AUTHORIZED_FUNDING",
  "jd_revision_id": "JD_REVISION_ID",
  "idempotency_key": "MY_EXTRACTION_REQUEST"
}
```

Import does not call a model. Extraction explicitly requests the server's
one-call metered extraction run. Inspect its eventual result with `tasks --run`.
A returned run ID is admission, not completion or semantic correctness. Reuse the
result's `goal_version_id` in a later authorized build; do not silently re-extract.

## Single-session plan and exact acceptance

Use the existing commands, without a second acceptance path:

```sh
careerforge learning session-propose ./session.json
careerforge tasks --run PLANNER_RUN_ID
careerforge learning plan PLAN_ID
careerforge learning accept PLAN_ID VERSION_ID EXPECTED_PLAN_REVISION --confirm ACCEPT
```

`session.json`:

```json
{
  "namespace_id": "PRIVATE_NAMESPACE",
  "funding_id": "ALREADY_AUTHORIZED_FUNDING",
  "publication_ids": ["REVIEWED_PUBLICATION_ID"],
  "learning_intent": "function",
  "title": "Read the reviewed implementation",
  "total_minutes": 30,
  "preferences": {
    "practice": false,
    "check_understanding": false,
    "requested_style": "code_first"
  },
  "expected_profile_revision": 1,
  "idempotency_key": "MY_SESSION_REQUEST"
}
```

Use the current profile revision, not the example value. The server enforces
total module duration and reviewed-source restrictions. A proposal is not
accepted automatically. Inspect the actual proposal's plan/version IDs and the
current plan revision before acceptance. The existing `--confirm ACCEPT` gate
remains required. No knowledge or too little available time produces an
`unbuildable` gap with `can_accept=false`, no model call and no active schedule.
That gap ID is not a plan ID. Session acceptance is a private relative learning
schedule, not an external calendar booking or a claim of learning completion.

## Provisional assessment, not mastery

```sh
careerforge assessment request ./assessment.json
careerforge tasks --run ASSESSMENT_RUN_ID
```

`assessment.json`:

```json
{
  "namespace_id": "PRIVATE_NAMESPACE",
  "funding_id": "ALREADY_AUTHORIZED_FUNDING",
  "attempt_id": "MY_SUBMITTED_ATTEMPT_ID",
  "idempotency_key": "MY_ASSESSMENT_REQUEST"
}
```

The server uses the owned submitted attempt, frozen rubric and actual authorized
evidence. The result remains a `model_assessment_candidate` with
`feedback.requires_review=true` and `competence_verified=false`. A statement such as
"tests passed" cannot replace a matching execution receipt. The CLI neither
creates human observations nor changes the learner's assistance declaration.

## Display the selected source, then optionally ACK

```sh
careerforge source-targets TURN_ID
careerforge source-show ANSWER_BLOCK_ID --citation-index 0
careerforge source-show ANSWER_BLOCK_ID --citation-index 0 --ack-key MY_DISPLAY_EVENT
careerforge learning navigation ANSWER_BLOCK_ID
```

`source-targets` exposes the exact answer block and zero-based citation index from
the owned turn's validated events. Invalidated blocks are excluded. It does not
mark anything displayed. `source-show` gets that specific navigation target from
`GET /api/v1/learning/navigation/{block}?citation_index=...`. It uses the server's
new `display` envelope containing the complete selected source lines, their range
and SHA-256, the file SHA-256 and `content_complete=true`. It checks the target,
range, text hash and actual line count before any selected source is printed.

The viewer renders every original selected line with its original line number
and a `>` selection marker (colored when supported), then flushes output. It
uses the verified fixed snapshot, not a local working-tree file or a generated
snippet. ANSI/OSC/control and bidi characters are filtered for terminal safety;
Rich markup stays literal. This is a numbered terminal selection view, not an
IDE/browser highlight. Paths in the response are only displayed, never opened.

Without `--ack-key`, the entire operation is read-only with respect to ACK state.
With an explicit key, only after rendering and flushing succeed does the client
send a `NavigationAckInput` to `POST /api/v1/learning/navigation-acks`. The body
matches the exact displayed block, citation index, source ID, commit, path and
full line range, using `client_event=source_highlight_ack`. Validation, access,
write, flush or interruption failures never trigger that POST. The command does
not retry a failed or uncertain POST. The server rechecks ownership, closure and
source integrity at acknowledgment time; revocation between display and POST
can therefore reject the receipt.

A receipt is explicitly `client_reported_not_server_observed`. It means the
client reports that its selected lines were output. It does not establish that
a human saw a terminal, understood the code, executed it, or mastered anything.
Redirecting output is still client output, not human observation. A retry with
the same key redisplays the source and requests the same idempotent receipt;
changing its target must not reuse that key. Server location alone, old receipt
state, target listing, ordinary chat rendering and a successful GET never create
a new ACK. The old manual JSON `learning navigation-ack` command remains explicit
and separate; it is not used as a shortcut by the viewer.

## Validation

```sh
python -m pytest -q tests/test_integration_cli_v166.py
python -m pytest -q tests/test_learning_cli_v16.py tests/test_control_cli_v16.py \
  tests/test_cli_resilience.py tests/test_chat_terminal.py
python -m ruff check src/careerforge/cli/integration_v166.py \
  src/careerforge/cli/main.py tests/test_integration_cli_v166.py
```

The new tests run the actual Typer commands. They cover exact endpoint/body
shapes, literal JD bytes and null metadata, required budgets, URL-segment safety,
terminal controls, missing/mismatched/hash-invalid windows, nonzero citation
indices, read-only listing/display, write/flush failure, and no premature ACK.
Additional tests use the actual main CLI and FastAPI routes to verify import,
extraction admission, unbuildable and buildable session proposals, explicit
version acceptance, provisional assessment readback, source revocation and
render-before-ACK/readback/idempotency. Their sources, planner and assessment
providers are explicitly synthetic. They prove client/service mechanics, not
live model semantics, a native GUI interaction, or learner outcomes.
