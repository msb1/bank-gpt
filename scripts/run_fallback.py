"""Capture live Qwen locator correction and rejected fallback cases."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

CAPABILITY_ID = "d847b7f4-6460-433f-b53c-4f4640c2d352"


def main() -> None:
    api = os.getenv("BANKGPT_API_URL", "http://127.0.0.1:8000")
    cases = [
        ("corrected_locator", "locator_drift", "success", "fallback_accepted"),
        ("missing_target", "missing_target", "intervention_required", "fallback_rejected"),
        ("risky_control", "risky_control", "intervention_required", "policy_blocked"),
    ]
    evidence = []
    with httpx.Client(base_url=api, timeout=150) as client:
        login = client.post("/auth/token", json={"username": "north_operator",
                                                  "password": "north-operator-pass"})
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        for name, fault, expected, event in cases:
            grant = client.post(f"/capabilities/{CAPABILITY_ID}/grants", headers=headers,
                                json={"tenant_id": 1})
            grant.raise_for_status()
            authorized = {**headers, "X-Secure-Permissions": grant.json()["secure_permissions"]}
            started_response = client.post(f"/capabilities/{CAPABILITY_ID}/replay", headers=authorized,
                                           json={"tenant_id": 1, "values": {"member_id": "10001"},
                                                 "scenario": {"fault": fault,
                                                              "step_id": "step_2" if fault == "risky_control" else "step_1"},
                                                 "assisted_fallback": True})
            started_response.raise_for_status()
            started = started_response.json()
            state_response = client.get(f"/runs/{started['run_id']}", headers=headers)
            state_response.raise_for_status()
            state = state_response.json()
            kinds = [item["kind"] for item in state["events"]]
            if started["result"]["status"] != expected or event not in kinds:
                raise RuntimeError(f"{name}: got {started['result']['status']} with events {kinds}")
            if name == "corrected_locator" and "checkpoint_verified" not in kinds:
                raise RuntimeError("corrected run missed checkpoint")
            if name == "risky_control" and "fallback_proposed" in kinds:
                raise RuntimeError("risky control reached fallback")
            evidence.append({"case": name, "scenario": {"fault": fault},
                             "run_id": started["run_id"], "status": started["result"]["status"],
                             "code": started["result"]["code"], "events": [
                                 {"kind": item["kind"], "step_id": item["detail"].get("step_id"),
                                  "outcome": item["detail"].get("outcome"),
                                  "reason": item["detail"].get("reason")}
                                 for item in state["events"] if item["kind"].startswith("fallback") or
                                 item["kind"] in {"checkpoint_verified", "policy_blocked"}]})
            client.delete(f"/runs/{started['run_id']}", headers=headers).raise_for_status()
    output = Path("evidence/fallback/live-qwen.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"model": "qwen2.5-7b-instruct-mlx",
                                  "cases": evidence}, indent=2) + "\n")
    print(json.dumps({"cases": [item["case"] for item in evidence], "evidence": str(output)}))


if __name__ == "__main__":
    main()
