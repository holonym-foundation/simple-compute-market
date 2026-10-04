#!/usr/bin/env python3
"""Mock external digest signer — stands in for `waap-cli sign-digest`.

Signs a 32-byte digest with the private key in $MOCK_SIGNER_KEY and prints the
65-byte r||s||v hex signature to stdout — the exact contract CommandSigner
expects (see alkahest sdks/rs command_signer.rs). This lets the WaaP escrow
path run end-to-end on a local chain before `waap-cli sign-digest` ships: the
SCM config holds NO raw key; only this external command does.

Usage: mock_sign_digest.py 0x<64-hex-digest>
"""

from __future__ import annotations

import os
import json
import sys


def main() -> int:
    typed_data = len(sys.argv) == 3 and sys.argv[1] == "--typed-data"
    if len(sys.argv) != 2 and not typed_data:
        print("usage: mock_sign_digest.py 0x<digest> | --typed-data <json>", file=sys.stderr)
        return 2
    key = os.environ.get("MOCK_SIGNER_KEY", "")
    if not key:
        print("MOCK_SIGNER_KEY not set", file=sys.stderr)
        return 2
    from eth_account import Account
    if typed_data:
        from eth_account.messages import encode_typed_data
        signed = Account.sign_message(encode_typed_data(full_message=json.loads(sys.argv[2])), key)
    else:
        digest_hex = sys.argv[1].strip()
        digest = bytes.fromhex(digest_hex[2:] if digest_hex.startswith("0x") else digest_hex)
        if len(digest) != 32:
            print(f"digest must be 32 bytes, got {len(digest)}", file=sys.stderr)
            return 2
        signed = Account.unsafe_sign_hash(digest, private_key=key)
    sig = signed.signature.hex()
    print(sig if sig.startswith("0x") else "0x" + sig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
