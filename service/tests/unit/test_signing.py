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
import json
import subprocess

import pytest

from service.signing import (
    WAAP_PREFIX,
    digest_command,
    external_signer_address,
    is_external_signer,
    make_alkahest_client,
    message_command,
    sign_message_eip191,
    typed_data_command,
    _message_signature,
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
    monkeypatch.delenv("ARKHAI_SIGNER_TYPED_DATA_CMD", raising=False)
    prog, args = digest_command()
    assert prog == "waap-cli" and "{digest}" in " ".join(args)
    prog, args = message_command()
    assert prog == "waap-cli" and "{message}" in " ".join(args)
    prog, args = typed_data_command()
    assert prog == "waap-cli" and "{typed_data}" in " ".join(args)


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
        def with_command_signer(
            program, args, address, rpc_url, address_config, typed_data_args=None
        ):
            calls["kind"] = "command"
            calls["program"] = program
            calls["args"] = args
            calls["address"] = address
            calls["typed_data_args"] = typed_data_args
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
    monkeypatch.setenv("ARKHAI_SIGNER_TYPED_DATA_CMD", "mock-signer typed {typed_data}")
    make_alkahest_client(f"{WAAP_PREFIX}{ADDR}", rpc_url="ws://x", address_config=None)
    assert calls["kind"] == "command"
    assert calls["program"] == "mock-signer"
    assert "{digest}" in " ".join(calls["args"])
    assert calls["address"] == ADDR
    # Path B: the typed-data command is threaded so escrow signs via sign-typed-data.
    assert "{typed_data}" in " ".join(calls["typed_data_args"])


@pytest.mark.parametrize("credential", ["waap:0x" + "zz" * 20, "waap:0x" + "00" * 20,
    "other:0x" + "ab" * 20, "waap: " + ADDR, "waap:" + ADDR + " "])
def test_external_signer_invalid_identity_refuses_before_command(monkeypatch, credential):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid identity invoked signer")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        external_signer_address(credential)
    if credential.startswith("waap:"):
        with pytest.raises(ValueError):
            sign_message_eip191("publish_listing:fixture:1", credential)


def test_external_result_is_verified_against_exact_message_and_identity(monkeypatch):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    account = Account.create()
    message = "publish_listing:fixture:1700000000"
    signature = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()
    signature = "0x" + signature.removeprefix("0x")
    calls = []
    def command(*args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"event": "result", "signature": signature}), stderr="")
    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setenv("ARKHAI_SIGNER_MESSAGE_CMD", "reviewed-signer message {message}")
    assert sign_message_eip191(message, "waap:" + account.address) == signature
    assert calls[0][0][0] == ["reviewed-signer", "message", message]
    assert calls[0][1]["timeout"] == 120
    with pytest.raises(RuntimeError, match="identity mismatch"):
        sign_message_eip191(message + "-changed", "waap:" + account.address)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        sign_message_eip191(message, "waap:" + Account.create().address)


@pytest.mark.parametrize("output", ["", "not-json", "0x" + "ab" * 64,
    json.dumps({"event": "error", "signature": "0x" + "ab" * 65}),
    json.dumps({"event": "result", "result": {"signature": "0x" + "ab" * 65}}),
    json.dumps({"event": "result", "signature": "0x" + "ab" * 65, "extra": True}),
    '{"event":"error","event":"result","signature":"0x' + "ab" * 65 + '"}',
    (json.dumps({"event": "result", "signature": "0x" + "ab" * 65}) + "\n") * 2,
    ("0x" + "ab" * 65 + "\n") * 2, "x" * 16385])
def test_message_result_rejects_ambiguous_or_malformed_output(output):
    with pytest.raises(RuntimeError, match="response unavailable"):
        _message_signature(output)


@pytest.mark.parametrize("failure", ["status", "timeout", "oserror", "encoding"])
def test_external_failure_is_sanitized_and_not_retried(monkeypatch, failure):
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["secret-argument"], 120, output="session-secret")
        if failure == "oserror":
            raise OSError("session-secret")
        if failure == "encoding":
            raise UnicodeDecodeError("utf-8", b"session-secret\xff", 14, 15, "invalid output")
        return types.SimpleNamespace(returncode=1, stdout="session-secret", stderr="session-secret")
    monkeypatch.setattr(subprocess, "run", command)
    with pytest.raises(RuntimeError, match="reconcile before retry") as exc:
        sign_message_eip191("publish_listing:fixture:1", "waap:" + ADDR)
    assert "secret" not in str(exc.value)
    assert len(calls) == 1


def test_typed_data_program_mismatch_refuses_sdk_construction(monkeypatch):
    calls = _stub_alkahest_py(monkeypatch)
    monkeypatch.setenv("ARKHAI_SIGNER_DIGEST_CMD", "digest-signer {digest}")
    monkeypatch.setenv("ARKHAI_SIGNER_TYPED_DATA_CMD", "other-signer {typed_data}")
    with pytest.raises(ValueError, match="same executable"):
        make_alkahest_client("waap:" + ADDR, rpc_url="http://invalid", address_config=None)
    assert calls == {}


def test_invalid_curve_signature_is_a_sanitized_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="0x" + "00" * 65, stderr=""))
    with pytest.raises(RuntimeError, match="signature invalid"):
        sign_message_eip191("publish_listing:fixture:1", "waap:" + ADDR)


@pytest.mark.parametrize("message", [None, "", "x" * 8193])
def test_invalid_message_refuses_before_command(monkeypatch, message):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid message invoked signer")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        sign_message_eip191(message, "waap:" + ADDR)
