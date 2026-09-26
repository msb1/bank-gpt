"""Generate and execute an immutable binding's pytest contract check with fictional data."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from bank_gpt.api import ARTIFACT_DIR
from bank_gpt.artifacts import ArtifactStore
from bank_gpt.codegen import generate


def main() -> None:
    api = os.getenv("BANKGPT_API_URL", "http://127.0.0.1:8000")
    with httpx.Client(base_url=api, timeout=30) as client:
        login = client.post("/auth/token", json={"username": "north_reader",
                                                  "password": "north-demo-pass"})
        login.raise_for_status()
        identity = login.json()["access_token"]
        operator = client.post("/auth/token", json={"username": "north_operator",
                                                     "password": "north-operator-pass"})
        operator.raise_for_status()
        operator_identity = operator.json()["access_token"]
        catalog = client.get("/capabilities/catalog", params={"tenant_id": 1},
                             headers={"Authorization": "Bearer " + identity})
        catalog.raise_for_status()
    item = next(item for item in catalog.json()["capabilities"]
                if item["id"] == "d847b7f4-6460-433f-b53c-4f4640c2d352")
    output = Path("evidence/codegen/generated_test.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = generate(ArtifactStore(ARTIFACT_DIR), item["id"], 1, item["binding_version"])
    if "10001" in source or "north-demo-pass" in source or identity in source:
        raise RuntimeError("generated source contains a literal example or token")
    output.write_text(source)
    input_data = {"capability_id": item["id"], "tenant_id": 1,
                  "binding_version": item["binding_version"],
                  "input_source": "BANKGPT_INPUT_MEMBER_ID environment variable"}
    (output.parent / "generator-input.json").write_text(json.dumps(input_data, indent=2) + "\n")
    env = {**os.environ, "BANKGPT_API_URL": api, "BANKGPT_IDENTITY_TOKEN": identity,
           "BANKGPT_OPERATOR_TOKEN": operator_identity,
           "BANKGPT_INPUT_MEMBER_ID": "10001"}
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", str(output)],
                               capture_output=True, text=True, env=env, timeout=120)
    report = {"generated_file": str(output), "exit_code": completed.returncode,
              "summary": completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "no output",
              "verified_output_and_checkpoint": completed.returncode == 0}
    (output.parent / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
