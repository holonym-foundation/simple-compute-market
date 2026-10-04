# Escrow Kind Codec Expansion

Scope and sequencing for supporting every escrow obligation shape under
`alkahest/contracts/src/obligations/escrow`.

## Current State

The market already treats escrow settlement as a codec dispatch problem:
`EscrowTerms` carries `escrow_contract` plus the concrete
`obligation_data`, and `service.clients.alkahest.get_escrow_codec_for`
resolves `(chain_name, escrow_address)` to an `EscrowKindCodec`.

Only `erc20_escrow_obligation_nontierable` is implemented today. Several
callers are still ERC20-shaped even though the wire model is more general:

- buyer proposal construction and escrow selection mostly reason about
  `token` and `amount`;
- listing display, pricing, and filtering use helpers like
  `accepted_token_address` and `primary_rate_value`;
- CSV escrow templates are easiest to express for one token plus one rate
  slot;
- buyer-side chain creation and seller-side settlement verification reject
  unknown codecs.

## Contract Coverage

### ERC20 non-tierable settlement identity boundary

Before inserting an escrow or scheduling provisioning, the storefront binds
the EAS recipient to the signature-bound `negotiation_threads.buyer`. A routing
URL, request claim, or the seller recipient encoded inside the arbiter demand
is not this identity. Historical negotiations without a verified buyer refuse
settlement; there is no automatic ownership backfill.

The SDK's `get_obligation` only reads EAS and ABI-decodes the data. The storefront
therefore checks the exact requested UID, buyer recipient, configured escrow
attester, expected schema UID, zero reference UID, `revocable=true`, timestamps,
and exact negotiated obligation data. Proposal/listing and selected client-map
chain must agree; a read cannot be relabeled as another chain afterwards.

Schema identity is pinned to the non-tierable constructor's exact string
`address arbiter, bytes demand, address token, uint256 amount`, resolver equal
to the configured escrow address, and revocable true. EAS derives its UID as
`keccak256(abi.encodePacked(schema, resolver, revocable))`. Source references:

- [ERC20 constructor and creation methods](https://github.com/arkhai-io/alkahest/blob/0411284c4726ee933fdedca33dd69abeca2078be/contracts/src/obligations/escrow/non-tierable/ERC20EscrowObligation.sol)
- [BaseAttester schema registration](https://github.com/arkhai-io/alkahest/blob/1bf46bb66ed38334208afe6eda48963c6823048a/contracts/src/BaseAttester.sol)
- [Pinned EAS schema UID](https://github.com/ethereum-attestation-service/eas-contracts/blob/558250dae4cb434859b1ac3b6d32833c6448be21/contracts/SchemaRegistry.sol#L52)
- [Pinned SDK getter](https://github.com/holonym-foundation/alkahest-py/blob/9d01fcbf9f1a6939220d2cb216e2e0aed9922041/rs/src/clients/obligations/erc20/escrow/non_tierable.rs#L30)

This trusts the operator's pinned deployment/address configuration and its
chain-bound client. It does not verify deployed bytecode, RPC chain identity,
finality, or the provenance of an SDK default address. Those remain deployment
acceptance requirements. A matching recipient is the buyer/refund beneficiary,
not necessarily the token payer: `doObligationFor` supports third-party funding.
Buyer-as-payer proof additionally needs the exact creation transaction/receipt;
the current UID-only settlement request does not provide it. Envelope validation
also does not strengthen RecipientArbiter into a compute-delivery guarantee or
authorize a signing/recovery step.

The admin dry-run `/admin/settle/{uid}/verify` and both client wrappers now require
`negotiation_id`. The stored negotiation must bind the requested listing, and its
buyer and proposal feed the same verifier. Ownerless or mismatched requests fail
closed. This endpoint remains read-only; supplied dry-run price/duration are not
authorization to provision. Old clients must provide the negotiated ID.

Alkahest escrow obligations currently split into tierable and non-tierable
variants of the same seven obligation shapes:

- ERC20: `arbiter`, `demand`, `token`, `amount`
- native token: `arbiter`, `demand`, `amount`
- ERC721: `arbiter`, `demand`, `token`, `tokenId`
- ERC1155: `arbiter`, `demand`, `token`, `tokenId`, `amount`
- token bundle: `arbiter`, `demand`, native amount, ERC20 arrays, ERC721 arrays,
  ERC1155 arrays
- attestation request: `arbiter`, `demand`, `attestation`
- attestation UID: `arbiter`, `demand`, `attestationUid`

The tierable and non-tierable variants may share the same `ObligationData`
layout, but they still need distinct codec kinds and SDK paths.

## Goals

- Register codecs for each escrow obligation kind that can resolve its chain
  address, create the on-chain obligation, and read it back for verification.
- Keep `EscrowTerms` as the negotiated settlement artifact; do not add an
  out-of-band escrow kind field to the wire model.
- Make unsupported escrow kinds fail explicitly with kind/address context.
- Keep listing-side `accepted_escrows` as the seller's advertised shape:
  `(chain_name, escrow_address, literal_fields, rates)`.
- Add tests at the codec boundary first, then representative end-to-end tests
  once listing/proposal semantics are stable.

## Non-Goals

- Do not implement every possible marketplace policy for every asset class in
  the codec layer. Codecs encode/decode and call the SDK; negotiation policy
  decides what is acceptable.
- Do not force non-token escrows into ERC20 helpers such as
  `accepted_token_address`.
- Do not require a full e2e scenario for every tierable/non-tierable variant
  before exposing the first non-ERC20 codec.

## Phases

### Phase 1: Codec Registry and Unit Tests

Add codec classes for the straightforward asset escrows:

- native token, non-tierable and tierable;
- ERC721, non-tierable and tierable;
- ERC1155, non-tierable and tierable;
- ERC20 tierable, if the SDK path is available and matches the existing
  non-tierable behavior.

Each codec should have unit tests for:

- address resolution against the Alkahest address book;
- `obligation_data` normalization, especially `demand` bytes;
- SDK call shape for create;
- decoded obligation shape used by seller verification.

### Phase 2: Listing and Proposal Semantics

Generalize the places that currently assume ERC20:

- escrow selection should match by `(chain_name, escrow_address)` and policy,
  not just token address;
- displayed price/rate helpers should expose a generic primary rate and only
  expose token-specific helpers for token escrows;
- CSV escrow templates should support multiple rate slots and array-valued
  literal fields where needed;
- buyer proposal construction should derive `literal_fields` and rate-bearing
  `fields` from the selected accepted escrow, not hard-code `token`/`amount`.

This phase should preserve current ERC20 behavior and error messages for the
existing compute buyer flow.

### Phase 3: Seller Verification

Extend settlement verification so the decoded on-chain obligation can be
compared against `EscrowTerms.obligation_data` for all registered codecs.
The verifier should remain a byte/field compare, not a policy dispatcher.

Tests should cover:

- unsupported codec rejection;
- matching decoded obligation data;
- mismatched literal fields;
- mismatched rate-bearing fields;
- tierable and non-tierable address dispatch.

### Phase 4: Representative E2E Coverage

Add compose-backed e2e coverage for representative non-ERC20 flows rather than
every contract variant. A practical first set:

- native token escrow;
- ERC721 or ERC1155 escrow;
- token bundle if listing/proposal templates are stable.

The goal is to prove the buyer/storefront/provisioning settlement path works
with non-ERC20 codecs, not to exhaustively test Alkahest itself.

### Phase 5: Attestation Escrows

Handle attestation-request and attestation-UID escrows after the product
semantics are explicit:

- who supplies the attestation data or UID;
- whether it is a literal field, a rate-like field, or negotiated message
  content;
- how the seller advertises acceptable schemas;
- how the buyer proves or selects the attestation before settlement.

These codecs can still be mechanically implemented earlier, but they should
not be treated as product-complete until those semantics are settled.

## Open Questions

- Which Alkahest SDK methods exist for each tierable path, and do their return
  receipts all expose `log.uid` consistently?
- Should `accepted_escrows.rates[*].field` support nested paths for bundle
  arrays, or should bundles require a richer typed template shape?
- Should registry filters gain first-class non-ERC20 axes such as
  `escrow_kind`, `token`, `tokenId`, or native amount, or should they stay as
  JSONPath filters over `accepted_escrows`?
- What is the minimum useful e2e matrix for release confidence without turning
  the integration suite into an Alkahest contract test suite?
