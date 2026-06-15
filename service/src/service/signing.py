"""Pluggable signing credential — raw private key OR an external command (WaaP).

A WaaP/MPC account has no exportable private key, so it can't flow through the
``private_key: str`` plumbing as a hex key. Instead of re-threading a config
object through every layer (negotiate → orchestrate → escrow), we keep the
string and change its meaning: when ``[wallet] signer = "waap"`` is set, the
resolved credential becomes the sentinel ``"waap:<address>"``. Every
intermediate layer passes it through untouched; only the three surfaces that
actually *use* the credential dispatch on it:

1. ``sign_message_eip191`` — negotiation/settle HTTP signatures (EIP-191 over
   a text message). Raw key → eth_account; waap → the message-signing command.
2. ``make_alkahest_client`` — on-chain escrow via alkahest-py. Raw key →
   ``AlkahestClient(private_key=...)``; waap →
   ``AlkahestClient.with_command_signer(...)`` (alkahest signs 32-byte digests
   through the external command; see holonym-foundation/alkahest-rs
   ``feat/external-signer-scm``).
3. (Seller mirror of 2 in ``market_storefront.services.alkahest_service``.)

Command backends are env-overridable so tests can substitute a mock signer and
deployments can pin exact CLI flags without code changes:

* ``ARKHAI_SIGNER_DIGEST_CMD``  — default ``waap-cli sign-digest {digest}``;
  receives the 0x-hex 32-byte digest via the ``{digest}`` token and must print
  a 65-byte hex ECDSA signature to stdout.
* ``ARKHAI_SIGNER_MESSAGE_CMD`` — default ``waap-cli sign-message {message}``;
  receives the raw text via ``{message}`` and must print a 65-byte hex EIP-191
  signature to stdout.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any, Optional


WAAP_PREFIX = "waap:"

_DEFAULT_DIGEST_CMD = "waap-cli sign-digest {digest}"
_DEFAULT_MESSAGE_CMD = "waap-cli sign-message {message}"


def is_external_signer(credential: Optional[str]) -> bool:
    """True when the credential is a ``waap:<address>`` sentinel, not a raw key."""
    return bool(credential) and credential.startswith(WAAP_PREFIX)


def external_signer_address(credential: str) -> str:
    """Extract the signer's 0x address from a ``waap:<address>`` credential."""
    addr = credential[len(WAAP_PREFIX):].strip()
    if not addr.startswith("0x") or len(addr) != 42:
        raise ValueError(
            f"malformed external-signer credential {credential!r}; expected "
            f"'waap:0x<40 hex chars>'"
        )
    return addr


def digest_command() -> tuple[str, list[str]]:
    """(program, args) for digest signing; args carry the ``{digest}`` token."""
    raw = os.environ.get("ARKHAI_SIGNER_DIGEST_CMD", "") or _DEFAULT_DIGEST_CMD
    parts = shlex.split(raw)
    return parts[0], parts[1:]


def message_command() -> tuple[str, list[str]]:
    """(program, args) for message signing; args carry the ``{message}`` token."""
    raw = os.environ.get("ARKHAI_SIGNER_MESSAGE_CMD", "") or _DEFAULT_MESSAGE_CMD
    parts = shlex.split(raw)
    return parts[0], parts[1:]


_DEFAULT_TYPED_DATA_CMD = "waap-cli sign-typed-data --data {typed_data}"


def typed_data_command() -> tuple[str, list[str]]:
    """(program, args) for EIP-712 typed-data signing; args carry the ``{typed_data}`` token.

    Preferred over digest signing for escrow: the structured EIP-712 payload is
    risk-assessable by the WaaP policy engine (and permission-token-scopable),
    unlike an opaque 32-byte digest. See holonym-foundation/internal-docs#1348.
    """
    raw = os.environ.get("ARKHAI_SIGNER_TYPED_DATA_CMD", "") or _DEFAULT_TYPED_DATA_CMD
    parts = shlex.split(raw)
    return parts[0], parts[1:]


def sign_message_eip191(message: str, credential: str) -> str:
    """EIP-191 sign a text message with whichever backend the credential names.

    Returns the 0x-prefixed hex signature. Raw-key path mirrors the historical
    inline eth_account code; the waap path shells to the message command (the
    command is responsible for the EIP-191 prefixing, matching how the seller
    verifies with ``encode_defunct(text=...)`` + recover).
    """
    if not is_external_signer(credential):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        sig = Account.sign_message(
            encode_defunct(text=message), credential,
        ).signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    program, args = message_command()
    argv = [program] + [a.replace("{message}", message) for a in args]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(
            f"external signer command {program!r} failed "
            f"(rc={out.returncode}): {out.stderr.strip()[:500]}"
        )
    sig = out.stdout.strip()
    if not sig:
        raise RuntimeError(f"external signer command {program!r} printed no signature")
    return sig if sig.startswith("0x") else "0x" + sig


def make_alkahest_client(
    credential: str,
    *,
    rpc_url: str,
    address_config: Any,
):
    """Construct an ``alkahest_py.AlkahestClient`` for the credential.

    Raw key → the classic ``AlkahestClient(private_key=...)``. ``waap:`` →
    ``AlkahestClient.with_command_signer(...)`` so escrow obligations are
    signed by the external command (no raw key in process). Late import —
    alkahest is heavyweight and tests monkeypatch this function.
    """
    from alkahest_py import AlkahestClient

    if not is_external_signer(credential):
        return AlkahestClient(
            private_key=credential,
            rpc_url=rpc_url,
            address_config=address_config,
        )

    address = external_signer_address(credential)
    program, args = digest_command()
    # Prefer the structured EIP-712 path (sign-typed-data) — alkahest forwards typed
    # data instead of an opaque digest, so the WaaP policy engine can risk-assess it.
    _, typed_data_args = typed_data_command()
    return AlkahestClient.with_command_signer(
        program, args, address, rpc_url, address_config,
        typed_data_args=typed_data_args,
    )
