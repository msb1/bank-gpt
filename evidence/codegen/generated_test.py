"""Generated from base d847b7f4-6460-433f-b53c-4f4640c2d352 and tenant binding version 4."""
import os
import httpx

CAPABILITY_ID = 'd847b7f4-6460-433f-b53c-4f4640c2d352'
TENANT_ID = 1
INPUT_NAMES = ['member_id']
INPUT_TYPES = {'member_id': 'string'}
OUTPUT_NAMES = ['balance']


def test_capability_contract():
    api_url = os.environ["BANKGPT_API_URL"]
    identity = os.environ["BANKGPT_IDENTITY_TOKEN"]
    values = {name: (os.environ["BANKGPT_INPUT_" + name.upper()] if INPUT_TYPES[name] == "string"
                     else __import__("json").loads(os.environ["BANKGPT_INPUT_" + name.upper()]))
              for name in INPUT_NAMES}
    headers = {"Authorization": "Bearer " + identity}
    with httpx.Client(base_url=api_url, timeout=90) as client:
        catalog = client.get("/capabilities/catalog", headers=headers, params={"tenant_id": TENANT_ID})
        catalog.raise_for_status()
        selected = next(item for item in catalog.json()["capabilities"] if item["id"] == CAPABILITY_ID)
        assert selected["binding_version"] == 4, "binding version changed"
        grant = client.post(f"/capabilities/{CAPABILITY_ID}/grants", headers=headers,
                            json={"tenant_id": TENANT_ID, "operation": "replay"})
        grant.raise_for_status()
        headers["X-Secure-Permissions"] = grant.json()["secure_permissions"]
        response = client.post(f"/capabilities/{CAPABILITY_ID}/replay", headers=headers,
                               json={"tenant_id": TENANT_ID, "values": values,
                                     "expected_binding_version": 4})
        response.raise_for_status()
        run = response.json()
        assert run["result"]["status"] == "success", run["result"]
        assert set(run["result"]["outputs"]) == set(OUTPUT_NAMES)
        state = client.get(f"/runs/{run['run_id']}", headers=headers)
        state.raise_for_status()
        assert any(e["kind"] == "checkpoint_verified" for e in state.json()["events"])
        operator_token = os.environ.get("BANKGPT_OPERATOR_TOKEN")
        if operator_token:
            closed = client.delete(f"/runs/{run['run_id']}", headers={
                "Authorization": "Bearer " + operator_token})
            closed.raise_for_status()
