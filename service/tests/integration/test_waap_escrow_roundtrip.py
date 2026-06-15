"""WaaP-signed escrow round-trip on a local chain — the WS1 chain, end to end.

Proves the SCM's actual escrow path executes a REAL on-chain ERC20 escrow with
NO raw key in the SCM's hands:

    service.signing.make_alkahest_client("waap:<addr>", ...)
      → alkahest_py.AlkahestClient.with_command_signer(...)
        → AlkahestSigner::Command (sdks/rs)
          → external digest command (mock_sign_digest.py, standing in for
            `waap-cli sign-digest`)
      → codec.create_obligation(...)  (the same call escrow_client._create makes)
        → approve tx + escrow-create tx land on anvil → EAS attestation uid

Requires `anvil` on PATH (EnvTestManager boots its own node and deploys the
Alkahest suite + mock tokens) and an alkahest_py build that exposes
`with_command_signer` (>= 0.5.0). Skips cleanly otherwise.

The mock command holds anvil's deterministic account #1 key — the point is the
SIGNING SEAM (alkahest shells out for every signature), not key custody of the
mock; `waap-cli sign-digest` swaps in as the production backend with zero SCM
code changes (ARKHAI_SIGNER_DIGEST_CMD).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import sys
import time

import pytest

anvil_missing = shutil.which("anvil") is None
alkahest_py = pytest.importorskip("alkahest_py")
needs_command_signer = not hasattr(alkahest_py.AlkahestClient, "with_command_signer")

pytestmark = [
    pytest.mark.skipif(anvil_missing, reason="anvil (foundry) not on PATH"),
    pytest.mark.skipif(
        needs_command_signer,
        reason="alkahest_py build lacks with_command_signer (need >= 0.5.0)",
    ),
]

# Anvil's deterministic account #1 — the "WaaP" account for this test. Only the
# mock signer process knows the key; the SCM side sees just the address.
WAAP_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
WAAP_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
SELLER_ADDR = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"  # anvil #2

FUNDING = 100 * 10**18
TRANSFER_CHUNK = 9 * 10**18  # MockERC20.transfer chunk size (see generate_state.py)

# Mirrors test-env/generate_state.py SECTION_FIELDS — the storefront's address
# config schema. Kept local so the test doesn't import a script outside the
# package.
SECTION_FIELDS: dict[str, list[str]] = {
    "arbiters_addresses": [
        "eas", "trivial_arbiter", "trusted_oracle_arbiter", "intrinsics_arbiter",
        "intrinsics_arbiter_2", "erc8004_arbiter", "any_arbiter", "all_arbiter",
        "attester_arbiter", "expiration_time_after_arbiter", "expiration_time_before_arbiter",
        "expiration_time_equal_arbiter", "recipient_arbiter", "ref_uid_arbiter",
        "revocable_arbiter", "schema_arbiter", "time_after_arbiter", "time_before_arbiter",
        "time_equal_arbiter", "uid_arbiter", "exclusive_revocable_confirmation_arbiter",
        "exclusive_unrevocable_confirmation_arbiter", "nonexclusive_revocable_confirmation_arbiter",
        "nonexclusive_unrevocable_confirmation_arbiter",
    ],
    "erc20_addresses": ["eas", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "payment_obligation"],
    "erc721_addresses": ["eas", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "payment_obligation"],
    "erc1155_addresses": ["eas", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "payment_obligation"],
    "native_token_addresses": ["eas", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "payment_obligation"],
    "token_bundle_addresses": ["eas", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "payment_obligation"],
    "attestation_addresses": ["eas", "eas_schema_registry", "barter_utils", "escrow_obligation_nontierable", "escrow_obligation_tierable", "escrow_obligation_2_nontierable", "escrow_obligation_2_tierable"],
    "string_obligation_addresses": ["eas", "obligation"],
    "commit_reveal_obligation_addresses": ["eas", "obligation"],
}


def _extract_addresses(env) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for section, fields in SECTION_FIELDS.items():
        section_obj = getattr(env.addresses, section)
        out[section] = {field: str(getattr(section_obj, field)) for field in fields}
    return out


def test_waap_escrow_roundtrip(tmp_path: pathlib.Path, monkeypatch) -> None:
    from alkahest_py import EnvTestManager, MockERC20

    from service.clients.alkahest import (
        build_payment_obligation_data,
        Erc20NonTierableEscrowCodec,
        get_alkahest_network,
        resolve_alkahest_address_config,
    )
    from service.signing import make_alkahest_client

    # 1. Local chain: boots anvil, deploys Alkahest + mock tokens.
    env = EnvTestManager()

    # 2. Address book → JSON override file (what [chains.anvil]
    #    alkahest_address_config_path points at in a real config).
    addr_path = tmp_path / "alkahest_anvil_addresses.json"
    addr_path.write_text(json.dumps(_extract_addresses(env)))

    # 3. Fund the WaaP account with MOCK (gas ETH is anvil-default-funded).
    mock = MockERC20(env.mock_addresses.erc20_a, env.god_wallet_provider)
    remaining = FUNDING
    while remaining > 0:
        chunk = min(remaining, TRANSFER_CHUNK)
        mock.transfer(WAAP_ADDR, chunk)
        remaining -= chunk
    assert int(mock.balance_of(WAAP_ADDR)) >= FUNDING

    # 4. External signer: the mock digest command holds the key; the SCM only
    #    ever sees the waap:<address> credential.
    mock_signer = pathlib.Path(__file__).parent / "mock_sign_digest.py"
    monkeypatch.setenv(
        "ARKHAI_SIGNER_DIGEST_CMD", f"{sys.executable} {mock_signer} {{digest}}"
    )
    monkeypatch.setenv("MOCK_SIGNER_KEY", WAAP_KEY)

    network = get_alkahest_network("anvil")
    address_config = resolve_alkahest_address_config(network, config_path=str(addr_path))
    client = make_alkahest_client(
        f"waap:{WAAP_ADDR}", rpc_url=env.rpc_url, address_config=address_config,
    )

    # 5. The exact path escrow_client._create runs: canonical obligation_data
    #    → codec.create_obligation (approve + escrow-create, both signed via
    #    the external command).
    obligation_data = build_payment_obligation_data(
        seller_wallet=SELLER_ADDR,
        agreed_amount=10**18,
        duration_seconds=3600,
        token_contract_address=str(env.mock_addresses.erc20_a),
        chain_name="anvil",
        addr_config_path=str(addr_path),
    )
    expiration = int(time.time()) + 7200
    codec = Erc20NonTierableEscrowCodec()
    uid = asyncio.run(codec.create_obligation(client, obligation_data, expiration))

    assert isinstance(uid, str) and uid.startswith("0x") and len(uid) == 66, uid
    assert int(uid, 16) != 0, "escrow uid is zero — attestation not created"

    # 6. Read the obligation back from chain via its attestation uid.
    obligation = asyncio.run(codec.get_obligation(client, uid))
    assert obligation is not None
