"""Tests for service.signing — the pluggable raw-key/WaaP signing credential.

The external path is exercised with a mock command (`echo <sig>`) so no
waap-cli, chain, or alkahest wheel is needed: we pre-compute a real EIP-191
signature with eth_account, have the mock command print it, and verify the
dispatched result recovers to the expected address — the same trick the
alkahest-rs CommandSigner unit test uses.
"""

from __future__ import annotations

import sys
import types

import pytest

from service.signing import (
    WAAP_PREFIX,
    digest_command,
    external_signer_address,
    is_external_signer,
    make_alkahest_client,
    message_command,
    sign_message_eip191,
)


ADDR = "0x" + "ab" * 20


def test_is_external_signer_detection():
    assert is_external_signer(f"{WAAP_PREFIX}{ADDR}")
    assert not is_external_signer("0x" + "11" * 32)  # raw key
    assert not is_external_signer("")
    assert not is_external_signer(None)


def test_external_signer_address_parses_and_rejects():
    assert external_signer_address(f"{WAAP_PREFIX}{ADDR}") == ADDR
    with pytest.raises(ValueError):
        external_signer_address(f"{WAAP_PREFIX}not-an-address")


def test_command_defaults_are_waap_cli(monkeypatch):
    monkeypatch.delenv("ARKHAI_SIGNER_DIGEST_CMD", raising=False)
    monkeypatch.delenv("ARKHAI_SIGNER_MESSAGE_CMD", raising=False)
    prog, args = digest_command()
    assert prog == "waap-cli" and "{digest}" in " ".join(args)
    prog, args = message_command()
    assert prog == "waap-cli" and "{message}" in " ".join(args)


def test_sign_message_raw_key_matches_eth_account():
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.create()
    sig = sign_message_eip191("hello:123", acct.key.hex())
    recovered = Account.recover_message(encode_defunct(text="hello:123"), signature=sig)
    assert recovered == acct.address


def test_sign_message_external_via_mock_command(monkeypatch):
    """waap path: mock command echoes a precomputed valid EIP-191 signature."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.create()
    message = "negotiate_new:listing-1:1700000000"
    expected_sig = Account.sign_message(
        encode_defunct(text=message), acct.key,
    ).signature.hex()
    if not expected_sig.startswith("0x"):
        expected_sig = "0x" + expected_sig

    # `echo` ignores the {message} token and just prints the signature.
    monkeypatch.setenv("ARKHAI_SIGNER_MESSAGE_CMD", f"echo {expected_sig}")

    sig = sign_message_eip191(message, f"{WAAP_PREFIX}{acct.address}")
    assert sig == expected_sig
    recovered = Account.recover_message(encode_defunct(text=message), signature=sig)
    assert recovered == acct.address


def test_sign_message_external_command_failure_raises(monkeypatch):
    monkeypatch.setenv("ARKHAI_SIGNER_MESSAGE_CMD", "false")
    with pytest.raises(RuntimeError):
        sign_message_eip191("msg", f"{WAAP_PREFIX}{ADDR}")


def _stub_alkahest_py(monkeypatch):
    """Install a stub alkahest_py module recording which constructor ran."""
    calls = {}

    class _StubClient:
        def __init__(self, *, private_key, rpc_url, address_config):
            calls["kind"] = "private_key"
            calls["private_key"] = private_key

        @staticmethod
        def with_command_signer(program, args, address, rpc_url, address_config):
            calls["kind"] = "command"
            calls["program"] = program
            calls["args"] = args
            calls["address"] = address
            return object()

    mod = types.ModuleType("alkahest_py")
    mod.AlkahestClient = _StubClient
    monkeypatch.setitem(sys.modules, "alkahest_py", mod)
    return calls


def test_make_alkahest_client_dispatches_raw_key(monkeypatch):
    calls = _stub_alkahest_py(monkeypatch)
    make_alkahest_client("0x" + "11" * 32, rpc_url="ws://x", address_config=None)
    assert calls["kind"] == "private_key"


def test_make_alkahest_client_dispatches_command_signer(monkeypatch):
    calls = _stub_alkahest_py(monkeypatch)
    monkeypatch.setenv("ARKHAI_SIGNER_DIGEST_CMD", "mock-signer sign {digest}")
    make_alkahest_client(f"{WAAP_PREFIX}{ADDR}", rpc_url="ws://x", address_config=None)
    assert calls["kind"] == "command"
    assert calls["program"] == "mock-signer"
    assert "{digest}" in " ".join(calls["args"])
    assert calls["address"] == ADDR
