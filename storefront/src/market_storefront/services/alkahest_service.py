"""AlkahestService — per-chain AlkahestClient factory.

Builds one ``AlkahestClient`` per configured ``[chains.<name>]`` entry.
Downstream call sites pick the right client by ``chain_name`` (sourced
from the listing's ``accepted_escrows[].chain_name`` or the incoming
escrow's chain).
"""
from __future__ import annotations

import logging
from typing import Any

from market_storefront.utils.config import CHAINS, settings

logger = logging.getLogger(__name__)


def build_clients() -> dict[str, Any]:
    """Build one ``AlkahestClient`` per chain in ``CHAINS``.

    Returns a dict keyed by chain name. Chains whose client fails to
    initialise (missing key, unreachable RPC, malformed address config)
    are omitted with a warning — the storefront keeps serving the
    chains it can.
    """
    # Resolve the signing credential: either a raw private key, or — when
    # ``[wallet] signer = "waap"`` is set — the ``waap:<address>`` sentinel
    # that routes signing through the external command (service.signing).
    signer_kind = str(settings.get("wallet.signer", "") or "").strip().lower()
    if signer_kind == "waap":
        addr = str(settings.get("wallet.address", "") or "").strip()
        if not addr:
            logger.warning(
                "[ALKAHEST] wallet.signer = \"waap\" requires wallet.address; "
                "no chain clients will be initialised."
            )
            return {}
        credential = f"waap:{addr}"
    else:
        credential = (settings.wallet.private_key or "").strip()
        if not credential:
            logger.warning(
                "[ALKAHEST] wallet.private_key not set; no chain clients will be "
                "initialised."
            )
            return {}
    if not CHAINS:
        logger.warning(
            "[ALKAHEST] no [chains.<name>] tables configured; nothing to build."
        )
        return {}

    from service.clients.alkahest import (
        get_alkahest_network,
        prewarm_alkahest_address_config_cache,
        resolve_alkahest_address_config,
    )
    from service.signing import make_alkahest_client

    out: dict[str, Any] = {}
    for name, cc in CHAINS.items():
        try:
            prewarm_alkahest_address_config_cache(cc.alkahest_address_config_path)
            network = get_alkahest_network(name)
            address_config = resolve_alkahest_address_config(
                network, config_path=cc.alkahest_address_config_path
            )
            out[name] = make_alkahest_client(
                credential,
                rpc_url=cc.rpc_url,
                address_config=address_config,
            )
            logger.info("[ALKAHEST] Client initialised for chain %s", name)
        except Exception as exc:
            logger.warning(
                "[ALKAHEST] Failed to initialise client for chain %s: %s. "
                "This chain will not be available at runtime.", name, exc,
            )
    return out
