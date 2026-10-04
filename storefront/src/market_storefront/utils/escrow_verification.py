"""Pre-settlement on-chain escrow verification.

The seller's storefront calls ``verify_escrow_for_settlement`` before any
provisioning side-effect. It reads the EAS attestation by uid via
alkahest-py's ``client.erc20.escrow.non_tierable.get_obligation(uid)`` and
asserts the on-chain obligation_data dict-matches what the seller
expects, computed via ``build_payment_obligation_data`` from the same
negotiation inputs the buyer used.

Verification is two-phase:

1. Attestation envelope: exact UID, persisted buyer beneficiary, configured
   escrow attester and its pinned schema, zero reference, revocable, live.
   The SDK only decodes EAS bytes: it does not authenticate this envelope.

2. Obligation data: the chain's ObligationData (arbiter + demand + token
   + amount for ERC20EscrowObligation) dict-equals the expected
   obligation_data byte-for-byte (modulo address-case normalization and
   bytes/hex normalization). Single dict-compare replaces the per-field
   hard-coded checks; adding new arbiter / escrow kinds later only
   requires updating ``build_payment_obligation_data`` (or its successor
   codec lookup in step 5), not this verifier.

The expected ``expiration_unix`` doesn't participate in dict-compare —
it's buyer-clock-stamped at escrow creation and the seller can't
reproduce it without the buyer publishing the value. Step 7 makes the
buyer publish the full EscrowTerms via the negotiation protocol so this
check can become exact-equal.

On any mismatch raises ``EscrowVerificationError``. The caller maps that
to HTTP 400 — settlement aborts before any DB side effect or chain
write. ``get_obligation_fn`` and ``build_obligation_data_fn`` are
injectable test seams.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


class EscrowVerificationError(ValueError):
    """Raised when an on-chain escrow does not match the negotiated terms."""


_ERC20_SCHEMA = "address arbiter, bytes demand, address token, uint256 amount"
_ZERO_UID = "0x" + "00" * 32


def _identity_hex(value: Any, size: int, label: str, *, nonzero: bool = True) -> str:
    if (not isinstance(value, str)
            or re.fullmatch(r"0x[0-9a-fA-F]{%d}" % (size * 2), value) is None
            or (nonzero and int(value[2:], 16) == 0)):
        raise EscrowVerificationError(f"Invalid {label}")
    return value.lower()


def erc20_escrow_schema_uid(escrow_address: str) -> str:
    """Pinned non-tierable schema, resolver=self, revocable=true (EAS packed UID).

    This trusts the configured deployment; it is not bytecode verification.
    """
    from eth_utils import keccak
    address = _identity_hex(escrow_address, 20, "configured escrow address")
    return "0x" + keccak(_ERC20_SCHEMA.encode() + bytes.fromhex(address[2:]) + b"\x01").hex()


def _normalize_address(addr: Any) -> str | None:
    """Lowercase address for case-insensitive comparison.

    Returns None when the input isn't a usable address string — caller
    distinguishes missing-from-listing (raise) vs. missing-on-chain
    (also raise, but with a different message).
    """
    if not addr or not isinstance(addr, str):
        return None
    return addr.lower()


def _normalize_bytes(value: Any) -> str | None:
    """Canonicalize a demand-bytes-like value to a "0x"-prefixed hex string.

    Accepts:
      - bytes / bytearray → hex-encode
      - "0x..."-prefixed hex string → lowercase
      - bare hex string (no 0x) → lowercase + prepend 0x

    Returns None for anything else (which the caller treats as a
    verification failure — chain reads should always produce one of
    the accepted shapes).
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, str):
        s = value.lower()
        if s.startswith("0x"):
            return s
        # tolerate bare hex (no leading 0x)
        try:
            bytes.fromhex(s)
            return "0x" + s
        except ValueError:
            return None
    return None


def _extract_token_contract_from_listing(listing: dict[str, Any]) -> str:
    """Pull the negotiated token contract address from the seller's
    listing's primary accepted-escrow entry.

    Used as the fallback when the buyer didn't include an
    ``escrow_proposal`` on the negotiation thread. With a proposal in
    hand the verifier reads ``proposal.literal_fields["token"]`` directly.
    """
    from service.schemas import accepted_token_address

    accepted = listing.get("accepted_escrows")
    if isinstance(accepted, str):
        try:
            accepted = json.loads(accepted)
        except Exception:
            accepted = None
    if isinstance(accepted, list) and accepted:
        addr = accepted_token_address(accepted[0])
        if addr:
            return addr
    raise EscrowVerificationError(
        "Cannot extract token contract address from listing — "
        "no accepted_escrows[0] token literal"
    )


def _normalize_obligation_data(data: dict[str, Any]) -> dict[str, Any]:
    """Canonical form for dict-compare.

    Addresses → lowercase, demand bytes → "0x"-prefixed hex, amount → int.
    Keys outside the canonical set pass through unchanged so we can
    spot-check shape-correctness alongside value-correctness.
    """
    out: dict[str, Any] = {}
    for key, val in data.items():
        if key in ("arbiter", "token"):
            out[key] = _normalize_address(val)
        elif key == "demand":
            out[key] = _normalize_bytes(val)
        elif key == "amount":
            out[key] = int(val) if val is not None else None
        else:
            out[key] = val
    return out


def _read_chain_obligation_data(obligation: Any) -> dict[str, Any]:
    """Read fields off the alkahest-py decoded ObligationData object.

    The SDK returns a typed struct (not a dict). We pull the four
    canonical ERC20EscrowObligation fields off it into a normalized
    dict so dict-compare can run.
    """
    return _normalize_obligation_data({
        "arbiter": getattr(obligation, "arbiter", None),
        "demand": (
            bytes(obligation.demand)
            if getattr(obligation, "demand", None) is not None
            else None
        ),
        "token": getattr(obligation, "token", None),
        "amount": getattr(obligation, "amount", None),
    })


async def verify_escrow_for_settlement(
    *,
    escrow_uid: str,
    expected_buyer: str,
    seller_wallet: str,
    agreed_price: int,
    agreed_duration_seconds: int,
    listing: dict[str, Any],
    alkahest_client: Any,
    chain_name: str,
    alkahest_address_config_path: str | None,
    escrow_proposal: Any = None,
    escrow_kind: str = "erc20_escrow_obligation_nontierable",
    now_unix: int | None = None,
    get_obligation_fn: Any = None,
    build_obligation_data_fn: Any = None,
) -> None:
    """Read the on-chain escrow and assert it matches the negotiated terms.

    Parameters
    ----------
    escrow_uid:
        The 0x-prefixed 32-byte attestation uid handed to us by the buyer.
    expected_buyer:
        Signature-bound buyer from the persisted negotiation, never a URL
        or an unauthenticated request claim. This is the escrow beneficiary,
        not proof of the token payer (doObligationFor supports third-party funding).
    seller_wallet:
        Our wallet address; participates in the expected obligation_data
        via the RecipientArbiter demand encoding.
    agreed_price, agreed_duration_seconds:
        From the negotiation thread; ``agreed_price`` is the absolute
        payment amount in base units (the DB column name is retained
        from before the per-hour → absolute refactor — semantically it
        is now the amount, not a rate). Together with the proposal's
        token + arbiter and the chain config they determine the entire
        expected obligation_data dict.
    listing:
        The seller's listing row (after ``load_listing``); used as the
        fallback source for the payment token when no proposal is
        available (legacy threads).
    alkahest_client:
        An ``AlkahestClient`` already bound to the right chain.
    chain_name, alkahest_address_config_path:
        Used to resolve trusted, pinned arbiter + escrow deployment addresses
        for the selected client chain (static lookup, no RPC or bytecode proof).
    escrow_proposal:
        The buyer's ``EscrowProposal``, persisted on the negotiation
        thread at /negotiate/new. When present, the verifier resolves
        the escrow-kind codec from ``(chain_name, escrow_address)`` and
        refuses to verify anything other than
        ``erc20_escrow_obligation_nontierable`` with
        ``NotImplementedError``. Token is read via
        ``accepted_token_address`` (literal_fields-first, legacy fields
        fallback). Arbiter override accepts either shape too. None for
        legacy threads — verifier falls back to the listing-derived
        token and the ``escrow_kind`` default.
    escrow_kind:
        Fallback escrow slot name when ``escrow_proposal`` is None.
        Today only ``"erc20_escrow_obligation_nontierable"`` is
        registered.
    now_unix:
        Override for ``time.time()`` (test seam).
    get_obligation_fn / build_obligation_data_fn:
        Test seams. ``get_obligation_fn`` defaults to the registered
        escrow-kind codec's ``get_obligation`` (returns the decoded
        ``{"attestation", "data"}`` shape). ``build_obligation_data_fn``
        defaults to the canonical helper that constructs the expected
        obligation_data dict.

    Raises
    ------
    EscrowVerificationError
        On any mismatch. Caller should map to HTTP 400.
    """
    if alkahest_client is None:
        raise EscrowVerificationError(
            "AlkahestClient not configured — cannot verify escrow on chain"
        )

    canonical_uid = _identity_hex(escrow_uid, 32, "escrow UID")
    if escrow_uid != canonical_uid:
        # The original UID is a durable DB/idempotency key at the caller.
        # Refuse alternate spellings rather than only normalizing this read.
        raise EscrowVerificationError("Escrow UID must be canonical lowercase hex")
    expected_buyer = _identity_hex(expected_buyer, 20, "persisted buyer")
    if not isinstance(chain_name, str) or not chain_name:
        raise EscrowVerificationError("Missing selected client chain")
    if escrow_proposal is not None and escrow_proposal.chain_name != chain_name:
        raise EscrowVerificationError("Escrow proposal chain differs from selected client chain")
    if escrow_proposal is None:
        accepted = listing.get("accepted_escrows")
        if isinstance(accepted, str):
            try:
                accepted = json.loads(accepted)
            except ValueError as exc:
                raise EscrowVerificationError("Invalid listing escrow configuration") from exc
        if (not isinstance(accepted, list) or not accepted
                or not isinstance(accepted[0], dict)
                or accepted[0].get("chain_name") != chain_name):
            raise EscrowVerificationError("Listing escrow chain differs from selected client chain")

    if build_obligation_data_fn is None:
        from service.clients.alkahest import (
            build_payment_obligation_data as build_obligation_data_fn,
        )

    # The proposal (when present) is the source of truth: its
    # (chain_name, escrow_address) identifies the escrow contract and
    # its literal_fields / fields supply the buyer-committed values.
    # Legacy threads with no proposal fall back to the kwarg defaults
    # + a listing-derived token.
    from service.schemas import (
        accepted_demands,
        accepted_recipient_address,
        accepted_token_address,
    )

    effective_arbiter_kind = "recipient_arbiter"
    effective_recipient = seller_wallet
    effective_demands: list[dict[str, Any]] = []
    _codec = None
    if escrow_proposal is not None:
        from service.clients.alkahest import (
            address_to_slot,
            get_escrow_codec_for,
        )
        _addr = (escrow_proposal.escrow_address or "").lower()
        # The buyer may leave the escrow contract unpinned — a zero-address
        # placeholder — so negotiation gates on field equality rather than a
        # specific (chain, address). An unpinned proposal escrows against the
        # chain's default kind, so resolve the codec from ``escrow_kind``
        # rather than the placeholder address (which matches no codec).
        _unpinned = (not _addr) or set(_addr.removeprefix("0x")) <= {"0"}
        if _unpinned:
            effective_escrow_kind = escrow_kind
        else:
            try:
                _codec = get_escrow_codec_for(
                    escrow_proposal.chain_name,
                    escrow_proposal.escrow_address,
                    config_path=alkahest_address_config_path,
                )
            except ValueError as exc:
                raise EscrowVerificationError(
                    f"Cannot resolve escrow codec for proposal "
                    f"(chain={escrow_proposal.chain_name!r}, "
                    f"address={escrow_proposal.escrow_address!r}): {exc}"
                ) from exc
            if _codec.kind != "erc20_escrow_obligation_nontierable":
                raise NotImplementedError(
                    f"Seller verify not implemented for escrow kind "
                    f"{_codec.kind!r} at address "
                    f"{escrow_proposal.escrow_address!r} "
                    f"(chain {escrow_proposal.chain_name!r}); "
                    f"ERC20 non-tierable only."
                )
            effective_escrow_kind = _codec.kind
        proposal_token = accepted_token_address(escrow_proposal)
        if not isinstance(proposal_token, str) or not proposal_token:
            raise EscrowVerificationError(
                f"escrow proposal for {escrow_uid} omitted token "
                f"(literal_fields['token'] missing); cannot verify "
                f"against chain"
            )
        effective_token = proposal_token
        proposal_literal = escrow_proposal.literal_fields or {}
        proposal_arbiter = proposal_literal.get("arbiter")
        if isinstance(proposal_arbiter, str) and proposal_arbiter:
            arbiter_slot = address_to_slot(
                escrow_proposal.chain_name, proposal_arbiter,
                config_path=alkahest_address_config_path,
            )
            if arbiter_slot:
                effective_arbiter_kind = arbiter_slot
        proposal_recipient = accepted_recipient_address(escrow_proposal)
        if proposal_recipient:
            effective_recipient = proposal_recipient
        effective_demands = accepted_demands(escrow_proposal)
    else:
        effective_escrow_kind = escrow_kind
        effective_token = _extract_token_contract_from_listing(listing)

    from service.clients.alkahest import get_escrow_kind_codec
    if _codec is None:
        try:
            _codec = get_escrow_kind_codec(effective_escrow_kind)
        except ValueError as exc:
            raise EscrowVerificationError(f"Cannot read escrow {escrow_uid}: {exc}") from exc
    if _codec.kind != "erc20_escrow_obligation_nontierable":
        raise EscrowVerificationError("Unsupported escrow envelope schema")
    try:
        expected_attester = _identity_hex(
            _codec.resolve_address(chain_name, config_path=alkahest_address_config_path),
            20, "configured escrow address",
        )
        selected_address = (escrow_proposal.escrow_address if escrow_proposal is not None
                            else accepted[0].get("escrow_address"))
        if selected_address and selected_address.lower() != "0x" + "00" * 20:
            if _identity_hex(selected_address, 20, "selected escrow address") != expected_attester:
                raise EscrowVerificationError("Selected escrow differs from configured deployment")
        expected_schema = erc20_escrow_schema_uid(expected_attester)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EscrowVerificationError(f"Cannot resolve escrow deployment: {exc}") from exc

    if get_obligation_fn is None:
        async def get_obligation_fn(client, uid):  # type: ignore[no-redef]
            return await _codec.get_obligation(client, uid)

    if not effective_recipient:
        raise EscrowVerificationError(
            "Escrow recipient is not configured — cannot verify escrow demand"
        )

    # Build the expected obligation_data via the same helper the buyer uses.
    # Any divergence between sides means a misconfigured chain/token/arbiter.
    try:
        expected_obligation_raw = build_obligation_data_fn(
            demands=effective_demands or None,
            recipient=effective_recipient,
            agreed_amount=int(agreed_price),
            duration_seconds=int(agreed_duration_seconds),
            token_contract_address=effective_token,
            chain_name=chain_name,
            addr_config_path=alkahest_address_config_path,
            arbiter_kind=effective_arbiter_kind,
        )
    except Exception as exc:
        raise EscrowVerificationError(
            f"Cannot construct expected obligation_data for chain={chain_name!r}: {exc}"
        ) from exc
    expected = _normalize_obligation_data(expected_obligation_raw)

    # Read the on-chain attestation + obligation.
    try:
        decoded = await get_obligation_fn(alkahest_client, escrow_uid)
    except Exception as exc:
        raise EscrowVerificationError(
            f"Failed to read escrow {escrow_uid} from chain: {exc}"
        ) from exc

    try:
        att = decoded["attestation"]
        obligation = decoded["data"]
        identities = (
            ("uid", 32, escrow_uid),
            ("recipient", 20, expected_buyer),
            ("attester", 20, expected_attester),
            ("schema", 32, expected_schema),
            ("ref_uid", 32, _ZERO_UID),
        )
        for field, size, expected_value in identities:
            actual_value = _identity_hex(getattr(att, field), size, field,
                                         nonzero=field != "ref_uid")
            if actual_value != expected_value:
                raise EscrowVerificationError(f"Escrow attestation {field} mismatch")
        if att.revocable is not True:
            raise EscrowVerificationError("Escrow attestation must be revocable")
        for field in ("time", "expiration_time", "revocation_time"):
            value = getattr(att, field)
            if type(value) is not int or not 0 <= value < 2**64:
                raise EscrowVerificationError(f"Invalid attestation {field}")
    except (KeyError, TypeError, AttributeError) as exc:
        raise EscrowVerificationError("Incomplete escrow attestation envelope") from exc

    # Attestation envelope checks (independent of obligation_data shape).
    if att.revocation_time:
        raise EscrowVerificationError(
            f"Escrow {escrow_uid} is revoked (revocation_time="
            f"{att.revocation_time})"
        )

    now = int(now_unix) if now_unix is not None else int(time.time())
    if att.expiration_time and int(att.expiration_time) <= now:
        raise EscrowVerificationError(
            f"Escrow {escrow_uid} expired at {att.expiration_time} "
            f"(now={now})"
        )
    if not att.expiration_time:
        # The EAS contract treats expiration_time=0 as "never expires";
        # for escrow obligations we always want a deadline so a stale
        # escrow can be reclaimed. Reject the no-expiry shape.
        raise EscrowVerificationError(
            f"Escrow {escrow_uid} has no expirationTime — refusing to settle"
        )
    if escrow_proposal is not None and int(att.expiration_time) != escrow_proposal.expiration_unix:
        raise EscrowVerificationError("Escrow expiration differs from the accepted proposal")

    # Dict-compare the canonical ObligationData. One check covers every
    # field the contract enforces at collection time (arbiter, demand,
    # token, amount) and adds nothing arbiter-specific to this verifier.
    actual = _read_chain_obligation_data(obligation)
    if actual != expected:
        # Build a focused diff so the operator sees exactly which fields
        # diverged. Stringify byte-y / large-int values for the message.
        diffs = []
        for key in sorted(set(actual) | set(expected)):
            if actual.get(key) != expected.get(key):
                diffs.append(
                    f"{key}: chain={actual.get(key)!r} expected={expected.get(key)!r}"
                )
        raise EscrowVerificationError(
            f"Escrow {escrow_uid} obligation_data mismatch: " + "; ".join(diffs)
        )

    logger.info(
        "[ESCROW_VERIFY] escrow=%s ok: amount=%s token=%s arbiter=%s exp=%s",
        escrow_uid, actual["amount"], actual["token"], actual["arbiter"],
        att.expiration_time,
    )
