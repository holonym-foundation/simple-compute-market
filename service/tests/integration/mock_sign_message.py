#!/usr/bin/env python3
"""Mock external message signer — stands in for `waap-cli sign-message`.

EIP-191 signs a text message with the private key in $MOCK_SIGNER_KEY and
prints the 65-byte hex signature to stdout — the contract
``service.signing.sign_message_eip191`` expects from the external command
(ARKHAI_SIGNER_MESSAGE_CMD). Companion to mock_sign_digest.py: together they
let the full buyer flow (negotiation + escrow) run with no raw key in the SCM
config before waap-cli ships.

Usage: mock_sign_message.py "<message text>"
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: mock_sign_message.py <message>", file=sys.stderr)
        return 2
    key = os.environ.get("MOCK_SIGNER_KEY", "")
    if not key:
        print("MOCK_SIGNER_KEY not set", file=sys.stderr)
        return 2

    from eth_account import Account
    from eth_account.messages import encode_defunct

    sig = Account.sign_message(encode_defunct(text=sys.argv[1]), key).signature.hex()
    print(sig if sig.startswith("0x") else "0x" + sig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
