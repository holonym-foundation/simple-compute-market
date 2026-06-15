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


# ---------------------------------------------------------------------------
# External tx submission (WaaP send-tx)
# ---------------------------------------------------------------------------
# A WaaP/MPC wallet can't drive alkahest's internal broadcast (the signer can't
# produce a raw-tx digest, and waap-cli builds its own tx). So for on-chain
# settlement we build the obligation calldata via alkahest's *_calldata methods
# and broadcast it with an external command (``waap-cli send-tx``), which signs
# under the WaaP MPC + lets blockaid simulate the tx (SOE-approved path,
# holonym-foundation/internal-docs#1348). Off when the env var is unset →
# alkahest's normal internal signing is used.

_SENDTX_CMD_ENV = "ARKHAI_SIGNER_SENDTX_CMD"
_SENDTX_RPC_ENV = "ARKHAI_SENDTX_RPC"


def sendtx_command() -> Optional[tuple[str, list[str]]]:
    """(program, args) for external tx submission; args carry ``{to}``/``{data}``.

    Returns None when unset. The deploy bakes the chain + RPC into the command,
    e.g. ``waap-cli send-tx --json --to {to} --data {data} --chain evm:84532
    --rpc https://base-sepolia-rpc.publicnode.com``.
    """
    raw = (os.environ.get(_SENDTX_CMD_ENV, "") or "").strip()
    if not raw:
        return None
    parts = shlex.split(raw)
    return parts[0], parts[1:]


def external_tx_submit_enabled() -> bool:
    """True when on-chain settlement should broadcast via the send-tx command."""
    return sendtx_command() is not None


def submit_tx_via_command(to: str, data: str) -> str:
    """Broadcast a tx via the external command; returns the 0x tx hash.

    ``to``/``data`` are the calldata target + 0x-hex input from an alkahest
    ``*_calldata`` method. The command must print JSON containing ``txHash``
    (``waap-cli send-tx --json`` does).
    """
    cmd = sendtx_command()
    if cmd is None:
        raise RuntimeError(f"{_SENDTX_CMD_ENV} not set")
    program, args = cmd
    argv = [program] + [a.replace("{to}", to).replace("{data}", data) for a in args]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(
            f"send-tx command {program!r} failed (rc={out.returncode}): "
            f"{out.stderr.strip()[:400]}"
        )
    import json
    tx_hash = None
    for line in out.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("txHash"):
            tx_hash = obj["txHash"]
    if not tx_hash:
        raise RuntimeError(
            f"send-tx command {program!r} printed no txHash: {out.stdout.strip()[:300]}"
        )
    return tx_hash


# EAS Attested(address indexed recipient, address indexed attester, bytes32 uid,
# bytes32 indexed schema) — uid is the sole non-indexed field, so it is the log
# data (last 32 bytes). Used to recover the fulfillment UID from a do_obligation
# tx broadcast externally.
def extract_attested_uid(tx_hash: str, *, rpc_url: Optional[str] = None, timeout: int = 240) -> str:
    """Recover the EAS attestation UID from an externally-broadcast tx receipt."""
    rpc = (rpc_url or os.environ.get(_SENDTX_RPC_ENV, "") or "").strip()
    if not rpc:
        raise RuntimeError(f"{_SENDTX_RPC_ENV} not set (needed to read the receipt)")
    if rpc.startswith("wss://"):
        rpc = "https://" + rpc[len("wss://"):]
    elif rpc.startswith("ws://"):
        rpc = "http://" + rpc[len("ws://"):]

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc))
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    attested_topic = bytes(w3.keccak(text="Attested(address,address,bytes32,bytes32)"))
    for log in receipt["logs"]:
        topics = log["topics"]
        if topics and bytes(topics[0]) == attested_topic:
            return "0x" + bytes(log["data"])[-32:].hex()
    raise RuntimeError(f"no EAS Attested event found in receipt for {tx_hash}")
