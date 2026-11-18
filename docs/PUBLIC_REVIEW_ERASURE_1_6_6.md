# Shared-public review authority during personal erasure

The r4 standalone consumer guard exposed an availability regression: personal
erasure correctly cleared a reviewer's `SourceRead.session_key`, while the new
guard required that exact original session on every replay. Valid shared-public
answer events were consequently converted to `answer.evidence_invalidated`.
The original privacy event-equality regression remains unchanged.

## Narrow preservation rule

Immediately before a confirmed erasure closes its privacy fence, the service may
record a `public-reviewed-read-erasure-1.6.6-v1` stage witness. Admission reuses
`runtime.is_public_system_run`: an active administrator payer, the same funding
payer and run principal, a shared-public namespace, system funding, and exactly
the `public_source` data class. The review must have succeeded. A public URL or
a caller-provided public label is not this authority.

Before creating a witness, the ordinary current publication guard revalidates the
original version, approved scoped-review contract, complete independent bundle,
Reviewer/Coverage responses, original request-access witnesses and authorization
closure. At least one fact must have actual positive independent standalone
review. Only reviewer reads already eligible for the existing, validated public
evidence-span retention rule receive witnesses.

The witness contains a contract ID, exact public publication ID, actual erasure
operation ID and one binding hash. That hash binds the publication, review,
version, original run/input, complete bundle, source/read identity and range,
source/read content hashes, original deterministic session hash, exact completed
role receipts and their access witnesses. It contains no source body, personal
session string, prompt, model response or new standalone verdict.

After erasure, a blank session alone grants nothing. The witness must match the
same publication/unit/run/read, a real progressed erasure and privacy generation,
and the current conservative public/system classification. The original bundle's
other fields, source, publication authorization, receipts and request-access
closure are still checked. Source, receipt, role, witness, contract, payer or
permission changes fail closed. The witness cannot be moved between units.
New source reads, evidence or review decisions are never manufactured.

## Intentional limits

- This does not reconstruct historical reads already erased without a witness.
- Personal, private, non-system, unknown-contract and invalid prior authority
  are never upgraded. All-false standalone reviews remain unusable atomically.
- An uncited exploratory read inside the original reviewer bundle is still
  erased under the existing policy. If the complete original bundle can no
  longer be revalidated, the new consumer pack is rejected. The still-public
  source is not substituted for the original read receipt. This release does
  not promise that every public publication remains atomically consumable after
  erasure. Legacy full-publication behavior is unchanged.
- No original session/path/text is restored, no retention class is broadened,
  no quarantined fact is revived, and no public API, model prompt or paid request
  is silently upgraded by this witness.
- The witness module participates in replay engine identity. It does not relax
  r3 execution/scoring or the cross-contract r4 promotion gate.

Tests use isolated synthetic service executions. They establish mechanical
privacy, authority, version and replay behavior, not native model quality,
learning benefit, production database concurrency or legal compliance.
