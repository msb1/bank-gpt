"""Run three live draft evaluations per tenant and approve from aggregate evidence."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from bank_gpt.api import ARTIFACT_DIR
from bank_gpt.artifacts import ArtifactStore

CAPABILITY_ID = "d847b7f4-6460-433f-b53c-4f4640c2d352"


async def tenant_review(client: httpx.AsyncClient, store: ArtifactStore,
                        tenant_id: int, username: str, password: str) -> dict:
    login = await client.post("/auth/token", json={"username": username, "password": password})
    login.raise_for_status()
    identity = login.json()["access_token"]
    headers = {"Authorization": "Bearer " + identity}
    prior = store.binding(CAPABILITY_ID, tenant_id)
    draft = prior.model_copy(update={"approval_state": "draft", "stability": None,
                                     "reviewed_by": None, "reviewed_at": None})
    revision = await client.post(
        f"/capabilities/{CAPABILITY_ID}/bindings/{tenant_id}/revisions",
        headers=headers, json=draft.model_dump(mode="json"))
    revision.raise_for_status()
    draft_version = revision.json()["version"]
    runs = []
    for index in range(1, 4):
        grant = await client.post(f"/capabilities/{CAPABILITY_ID}/grants", headers=headers,
                                  json={"tenant_id": tenant_id, "operation": "evaluate"})
        grant.raise_for_status()
        replay_headers = {**headers, "X-Secure-Permissions": grant.json()["secure_permissions"]}
        response = await client.post(f"/capabilities/{CAPABILITY_ID}/replay", headers=replay_headers,
                                     json={"tenant_id": tenant_id,
                                           "values": {"member_id": str(tenant_id * 10000 + index)},
                                           "evaluation": True})
        response.raise_for_status()
        data = response.json()
        if data["result"]["status"] != "success":
            raise RuntimeError(f"tenant {tenant_id} evaluation failed: {data['result']['status']}")
        runs.append({"run_id": data["run_id"], "status": data["result"]["status"],
                     "output_names": list(data["result"]["outputs"])})
    report_response = await client.post(f"/capabilities/{CAPABILITY_ID}/stability", headers=headers,
                                        json={"tenant_id": tenant_id,
                                              "run_ids": [item["run_id"] for item in runs]})
    report_response.raise_for_status()
    report = report_response.json()
    if report["rate"] != 1 or report["counts"]["success"] != 3:
        raise RuntimeError("stability threshold not met")
    approve = await client.post(f"/capabilities/{CAPABILITY_ID}/bindings/{tenant_id}/approve",
                                headers=headers)
    approve.raise_for_status()
    binding = approve.json()
    for item in runs:
        closed = await client.delete(f"/runs/{item['run_id']}", headers=headers)
        closed.raise_for_status()
    return {"tenant_id": tenant_id, "base_id": CAPABILITY_ID,
            "draft_binding_version": draft_version,
            "approved_binding_version": binding["version"],
            "approval_state": binding["approval_state"],
            "reviewed_by": binding["reviewed_by"], "reviewed_at": binding["reviewed_at"],
            "individual_runs": runs, "aggregate": report}


async def main() -> None:
    api = os.getenv("BANKGPT_API_URL", "http://127.0.0.1:8000")
    store = ArtifactStore(ARTIFACT_DIR)
    async with httpx.AsyncClient(base_url=api, timeout=90) as client:
        north = await tenant_review(client, store, 1, "north_operator", "north-operator-pass")
        pine = await tenant_review(client, store, 2, "pine_operator", "pine-operator-pass")
    output = Path("evidence/stability/review.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"N": 3, "tenants": [north, pine]}, indent=2) + "\n")
    print(json.dumps({"tenants": [1, 2], "runs": 6, "evidence": str(output)}))


if __name__ == "__main__":
    asyncio.run(main())
