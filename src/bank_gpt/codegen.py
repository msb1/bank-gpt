"""Generate a pytest replay contract check for one immutable artifact/binding version."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import ArtifactStore


def generate(store: ArtifactStore, capability_id: str, tenant_id: int, binding_version: int) -> str:
    base = store.base(capability_id)
    path = store.binding_version_path(capability_id, tenant_id, binding_version)
    if not path.exists():
        raise ValueError("immutable binding version not found")
    from .models import TenantBinding
    binding = TenantBinding.model_validate_json(path.read_text())
    binding.resolve(base)
    names = [item.name for item in base.inputs]
    input_types = {item.name: item.type.value for item in base.inputs}
    output_names = [item.name for item in base.outputs]
    return f'''"""Generated from base {base.id} and tenant binding version {binding.version}."""
import os
import httpx

CAPABILITY_ID = {base.id!r}
TENANT_ID = {tenant_id}
INPUT_NAMES = {names!r}
INPUT_TYPES = {input_types!r}
OUTPUT_NAMES = {output_names!r}


def test_capability_contract():
    api_url = os.environ["BANKGPT_API_URL"]
    identity = os.environ["BANKGPT_IDENTITY_TOKEN"]
    values = {{name: (os.environ["BANKGPT_INPUT_" + name.upper()] if INPUT_TYPES[name] == "string"
                     else __import__("json").loads(os.environ["BANKGPT_INPUT_" + name.upper()]))
              for name in INPUT_NAMES}}
    headers = {{"Authorization": "Bearer " + identity}}
    with httpx.Client(base_url=api_url, timeout=90) as client:
        catalog = client.get("/capabilities/catalog", headers=headers, params={{"tenant_id": TENANT_ID}})
        catalog.raise_for_status()
        selected = next(item for item in catalog.json()["capabilities"] if item["id"] == CAPABILITY_ID)
        assert selected["binding_version"] == {binding.version}, "binding version changed"
        grant = client.post(f"/capabilities/{{CAPABILITY_ID}}/grants", headers=headers,
                            json={{"tenant_id": TENANT_ID, "operation": "replay"}})
        grant.raise_for_status()
        headers["X-Secure-Permissions"] = grant.json()["secure_permissions"]
        response = client.post(f"/capabilities/{{CAPABILITY_ID}}/replay", headers=headers,
                               json={{"tenant_id": TENANT_ID, "values": values,
                                     "expected_binding_version": {binding.version}}})
        response.raise_for_status()
        run = response.json()
        assert run["result"]["status"] == "success", run["result"]
        assert set(run["result"]["outputs"]) == set(OUTPUT_NAMES)
        state = client.get(f"/runs/{{run['run_id']}}", headers=headers)
        state.raise_for_status()
        assert any(e["kind"] == "checkpoint_verified" for e in state.json()["events"])
        operator_token = os.environ.get("BANKGPT_OPERATOR_TOKEN")
        if operator_token:
            closed = client.delete(f"/runs/{{run['run_id']}}", headers={{
                "Authorization": "Bearer " + operator_token}})
            closed.raise_for_status()
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capability_id")
    parser.add_argument("tenant_id", type=int)
    parser.add_argument("binding_version", type=int)
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = generate(ArtifactStore(args.artifact_dir), args.capability_id,
                      args.tenant_id, args.binding_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source)
    args.output.with_suffix(".input.json").write_text(json.dumps({
        "capability_id": args.capability_id, "tenant_id": args.tenant_id,
        "binding_version": args.binding_version}, indent=2) + "\n")


if __name__ == "__main__":
    main()
