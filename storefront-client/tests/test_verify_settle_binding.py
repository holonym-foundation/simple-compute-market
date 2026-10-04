"""Admin dry-run wrappers retain the required persisted negotiation binding."""
import asyncio
import json

import httpx
import pytest

from storefront_client.client import StorefrontClient, SyncStorefrontClient


PARAMS = dict(negotiation_id="neg-1", seller_wallet="0x" + "11" * 20,
              agreed_price=1000, agreed_duration_seconds=3600,
              listing_id="listing-1", chain_name="anvil")
UID = "0x" + "ab" * 32


@pytest.mark.parametrize("synchronous", [True, False])
def test_admin_verify_retains_negotiation_and_refuses_omission(synchronous):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"valid": True, "escrow_uid": UID})

    transport = httpx.MockTransport(handle)
    missing = {key: value for key, value in PARAMS.items() if key != "negotiation_id"}
    if synchronous:
        with SyncStorefrontClient("http://test", transport=transport, admin_key="synthetic") as client:
            with pytest.raises(TypeError):
                client.verify_settle(UID, **missing)
            assert client.verify_settle(UID, **PARAMS)["valid"] is True
    else:
        async def run():
            async with StorefrontClient("http://test", transport=transport, admin_key="synthetic") as client:
                with pytest.raises(TypeError):
                    await client.verify_settle(UID, **missing)
                assert (await client.verify_settle(UID, **PARAMS))["valid"] is True
        asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.path == f"/api/v1/admin/settle/{UID}/verify"
    assert json.loads(requests[0].content) == PARAMS
