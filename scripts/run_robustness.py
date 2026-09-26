"""Capture and assert live demo replay scenarios through the HTTP API."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
from bank_gpt.publication import public_text


CASES = [
    ("success", "10001", None, "success", None),
    ("other_tenant_member", "20001", None, "business_outcome", "member_not_found"),
    ("permission_denied", "403", None, "failure", "permission_denied"),
    ("validation_error", "10001", {"fault": "validation_error", "step_id": "step_2"}, "business_outcome", "validation_error"),
    ("session_recovered", "10001", {"fault": "session_expired", "step_id": "step_2"}, "success", None),
    ("session_exhausted", "10001", {"fault": "session_expired", "step_id": "step_2", "occurrences": 2}, "intervention_required", "recovery_exhausted"),
    ("slow_within_limit", "10001", {"fault": "slow_load", "step_id": "step_2", "delay_seconds": 2}, "success", None),
    ("slow_beyond_limit", "10001", {"fault": "slow_load", "step_id": "step_2", "delay_seconds": 8}, "intervention_required", "wait_limit_exceeded"),
    ("application_error", "10001", {"fault": "app_error", "step_id": "step_2"}, "failure", "application_error"),
    ("missing_target", "10001", {"fault": "missing_target", "step_id": "step_1"}, "intervention_required", "target_unavailable"),
    ("ambiguous_target", "10001", {"fault": "ambiguous_target", "step_id": "step_1"}, "intervention_required", "target_unavailable"),
    ("disallowed_destination", "10001", {"fault": "disallowed_destination", "step_id": "step_2"}, "intervention_required", "policy_blocked"),
    ("risky_control", "10001", {"fault": "risky_control", "step_id": "step_2"}, "intervention_required", "policy_blocked"),
]


def scrub(value: object, member: str) -> object:
    if isinstance(value, dict):
        return {key: scrub(item, member) for key, item in value.items()
                if key not in {"access_token", "password"}}
    if isinstance(value, list):
        return [scrub(item, member) for item in value]
    if isinstance(value, str):
        value = value.replace(member, "[input]")
        value = re.sub(r"\$\s?\d[\d,]*\.\d\d", "[amount]", value)
        return public_text(re.sub(r"\b\d{5,}\b", "[number]", value))
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--capability", required=True)
    parser.add_argument("--output", type=Path, default=Path("evidence/robustness"))
    parser.add_argument("--username", default="north_operator")
    parser.add_argument("--password", default="north-operator-pass")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=args.api, timeout=45) as client:
        login = client.post("/auth/token", json={"username": args.username, "password": args.password})
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        for name, member, scenario, status, code in CASES:
            grant_response = client.post(f"/capabilities/{args.capability}/grants",
                                         json={"tenant_id": 1}, headers=headers)
            grant_response.raise_for_status()
            headers["X-Secure-Permissions"] = grant_response.json()["secure_permissions"]
            payload = {"tenant_id": 1, "values": {"member_id": member}}
            if scenario:
                payload["scenario"] = scenario
            response = client.post(f"/capabilities/{args.capability}/replay", json=payload, headers=headers)
            response.raise_for_status()
            started = response.json()
            run_id = started["run_id"]
            try:
                state_response = client.get(f"/runs/{run_id}", headers=headers)
                state_response.raise_for_status()
                state = state_response.json()
                result = started["result"]
                assert (result["status"], result["code"]) == (status, code), (name, result)
                events = state["events"]
                if status == "success":
                    assert "balance" in result["outputs"]
                    assert any(e["kind"] == "checkpoint_verified" for e in events)
                if name == "permission_denied":
                    assert result["step_id"] == "step_3"
                    assert result["observed"] == "Permission denied"
                if name == "session_recovered":
                    assert any(e["kind"] == "recovery" for e in events)
                if name in {"missing_target", "ambiguous_target", "slow_beyond_limit"}:
                    assert any(e["kind"] == "failure_observation" for e in events)
                if name in {"disallowed_destination", "risky_control"}:
                    assert any(e["kind"] == "policy_blocked" for e in events)
                    assert not any(e["kind"] == "step_completed" and e["detail"].get("step_id") == "step_2" for e in events)
                if status in {"failure", "intervention_required", "business_outcome"}:
                    assert result["step_id"] is not None
                manifest = {"name": name, "capability_id": args.capability,
                            "scenario": scenario, "expected_status": status, "expected_code": code,
                            "run_id": run_id, "assertion": "passed", "browser": "remote-playwright"}
                case_dir = args.output / name
                case_dir.mkdir(parents=True, exist_ok=True)
                (case_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
                (case_dir / "result.json").write_text(json.dumps(scrub(started, member), indent=2) + "\n")
                (case_dir / "events.json").write_text(json.dumps(scrub(events, member), indent=2) + "\n")
                observation = next((e["detail"] for e in reversed(events)
                                    if e["kind"] == "failure_observation"), None)
                if observation is None:
                    observation = next((e["detail"] for e in reversed(events)
                                        if e["kind"] in {"intervention_requested", "hard_failure"}),
                                       {"available": False, "reason": "no failure in this run"})
                (case_dir / "observation.json").write_text(json.dumps(scrub(observation, member), indent=2) + "\n")
                print(f"{name}: {status} {code or ''} ({run_id})")
            finally:
                close = client.delete(f"/runs/{run_id}", headers=headers)
                close.raise_for_status()


if __name__ == "__main__":
    main()
