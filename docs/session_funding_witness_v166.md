# New Session funding identity witness

Status: offline candidate contract, not production, full-regression or native
verification. No real purchase, funding authorization or provider call is made
by its synthetic tests.

## Scope and reason

The existing runtime intentionally preserves receipts for requests already issued
before `revoke_funding`. That service cancels nonterminal runs, advances epochs,
interrupts their turns and blocks later delivery and work. Preserving an issued
legacy receipt is not evidence of continued business delivery or a privacy leak.
No business API to change a funding payer was found. Direct database mutations
test defense against corrupted runtime identity, not a user-reachable payment API.

Only new Session admissions using `reviewed-citation-checker-1.6.6-v4` adopt
`session-funding-identity-1.6.6-v1`. Their policy matches the new ordinary checker:
current funding must still be active and unexpired before accepting a newly
received body. This is an explicit new contract, not a global runtime change.

## Server authority

The existing Session selection snapshot adds `session_funding_witness` containing
the contract, exact requested funding ID, admitted payer and namespace. With no
funding, both funding and payer IDs are null and no run may be attached. The payer
is the authenticated owner, consistent with the existing runtime admission check.

The Session token adds one field of the same name containing only the contract
and the authority hash. Its old fields remain intact. An independent server
idempotency receipt, operation `conversation.session_funding`, stores the original
authority, full binding and actual run ID under the turn ID. Its run ID is sealed
once inside the original admission transaction, or null without funding. No HTTP/model field supplies this
authority. The existing snapshot, turn, run, learning-session and completion/ACK
binding continue to agree on the full token.

All markers, hashes, the original request, the independent receipt and the run's
funding ID, manifest ID, input hash, owner, namespace, workflow and turn ID must
agree. Current funding is freshly read and compared to the admitted identity.
The gateway locates new bound turns from that independent authority's run ID and
also checks `Turn.run_id`. Removing both mutable forward/reverse hints or changing
the workflow cannot turn one into an unbound model call. Stripping
and resealing old Session mirrors does not erase the independent new authority.
As elsewhere, an attacker who can rewrite every database authority is outside
the integrity guarantee; these are server-controlled records, not signatures
protecting against total database compromise.

Funding/payer identifiers remain local. Checker evidence still contains exactly
one reviewed fact and its citations; there is no added financial text or grant of
source access. The implementation participates in the existing checker identity,
request/cache/replay identity and evolution version. Version 4 pending work cannot
be substituted for earlier marked implementations or approved by old evaluations.

## Four execution boundaries and history

The existing `_input_witnesses` path performs validation before reservation or
paid-receipt reuse, immediately before provider dispatch, and before a newly
received body is persisted. The runtime lease path also validates new Session
authority. Global `active_funding`, gateway settlement and payer creation are
unchanged. The ordinary checker's matching independent run-admission anchor is
described separately in `ordinary_checker_contract_v166.md`.

- A changed or inactive/expired grant before dispatch causes refusal and no
  provider request. A reservation that already exists is not silently refunded.
- A change while a new-contract call is in flight preserves its issued receipt
  and legitimate usage, but the new body is marked redacted and not delivered.
- A body completed before later revocation remains stored under its original
  receipt and payer. A pending attempt to reuse it requires current authority and
  is refused without another provider call or rewriting that old receipt.
- Read-only completed Session history validates the original server binding,
  current ACL/privacy and evidence, but does not require authorization for new
  paid work. Reading history does not execute the run or restore withdrawn funds.
- Unmarked C3 Session and ordinary requests keep their original wire and receipt
  rules. No migration inserts the new witness. Earlier marked candidate versions
  fail the existing implementation-identity check rather than being upgraded.

The new marker does not loosen source scope, roles/data classes, privacy, r4
isolation, byte budgets, call caps, no-practice preferences, semantic support or
completion/acknowledgement gates. Missing evidence remains a refusal or gap.
