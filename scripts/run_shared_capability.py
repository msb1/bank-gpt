"""Exercise live base discovery and two tenant bindings through remote Playwright."""
from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path

import httpx

from bank_gpt.api import ARTIFACT_DIR, PLAYWRIGHT_URL, POLICY
from bank_gpt.artifacts import ArtifactStore
from bank_gpt.core import RunSession, discover, replay
from bank_gpt.driver import BrowserSurface
from bank_gpt.models import DiscoveryRequest, Locator, Parameter, Target, TenantBinding
from bank_gpt.publication import public_data
from bank_gpt.tenant import GATEWAY, issue_token

API = "http://127.0.0.1:8000"
ORIGIN = sorted(POLICY.allowed_origins)[0]


class ObservedFlow:
    """Explicit test decisions based on live controls when the model is unavailable."""

    async def decide(self, goal, observation, inputs, outputs, completed, recent_steps):
        controls = observation["controls"]
        def index(predicate):
            return next(item["index"] for item in controls if predicate(item))
        if not recent_steps:
            return {"action": "type", "index": index(lambda item: item["name"] == "member_id"),
                    "parameter": "member_id", "reason": "member input observed"}
        if len(recent_steps) == 1:
            return {"action": "click", "index": index(lambda item: item["tag"] == "button" and item["text"] == "Search"),
                    "reason": "search button observed"}
        if len(recent_steps) == 2:
            return {"action": "extract", "index": index(lambda item: item["row_label"] == "Savings balance"),
                    "output": "balance", "reason": "savings row observed"}
        return {"action": "finish", "checkpoint_index": index(lambda item: item["tag"] == "h2" and item["text"] == "Member detail"),
                "reason": "member detail heading observed"}


def pine_binding(base_id: str, source: TenantBinding) -> TenantBinding:
    targets = dict(source.targets)
    targets["member_input"] = Target(description="Customer reference", candidates=[
        Locator(strategy="name", value="customer_ref"), Locator(strategy="id", value="pine-reference")])
    targets["search_button"] = Target(description="Find account", candidates=[
        Locator(strategy="text", value="Find account", tag="button")])
    targets["balance_cell"] = Target(description="Savings available", candidates=[
        Locator(strategy="css", value="strong[data-role='savings-balance']")])
    targets["success_marker"] = Target(description="Account overview", candidates=[
        Locator(strategy="text", value="Account overview", tag="h2")])
    return TenantBinding(base_id=base_id, tenant_id=2, entry_url=f"{ORIGIN}/demo/pine",
                         allowed_origin=ORIGIN, ui_fingerprint="Pine Valley Account Desk",
                         ui_version="pine-v2",
                         targets=targets, approval_state="draft")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", help="replay an already approved base without rediscovery")
    parser.add_argument("--model-discover", action="store_true",
                        help="discover through the configured model and the HTTP API")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        args.output = Path("evidence/shared-capability/model-live-reuse.json" if args.model_discover
                           else "evidence/shared-capability/live-reuse.json")
    output = args.output.parent
    output.mkdir(parents=True, exist_ok=True)
    actor = GATEWAY.authenticate("shared_reader", "shared-demo-pass")
    assert actor
    if args.existing and args.model_discover:
        parser.error("--existing and --model-discover are mutually exclusive")
    discovery_trace = None
    if not args.existing:
        request = DiscoveryRequest(goal="Look up a member and read the current savings balance",
                               tenant_id=1, target_url=f"{ORIGIN}/demo",
                               capability_name="read_savings_balance",
                               inputs=[Parameter(name="member_id", type="string", description="Member identifier")],
                               values={"member_id": "10001"},
                               outputs=[Parameter(name="balance", type="string", description="Displayed savings balance")])
        if args.model_discover:
            async with httpx.AsyncClient(base_url=API, timeout=180) as discovery_client:
                login_response = await discovery_client.post("/auth/token", json={
                    "username": "shared_reader", "password": "shared-demo-pass"})
                login_response.raise_for_status()
                reader_headers = {"Authorization": "Bearer " + login_response.json()["access_token"]}
                response = await discovery_client.post("/discover", headers=reader_headers,
                                                       json=request.model_dump(mode="json"))
                response.raise_for_status()
                started = response.json()
                state_response = await discovery_client.get(f"/runs/{started['run_id']}",
                                                            headers=reader_headers)
                state_response.raise_for_status()
                if started["result"]["status"] != "success":
                    decisions = [{"action": event["detail"].get("action"),
                                  "output": event["detail"].get("output"),
                                  "index": event["detail"].get("index")}
                                 for event in state_response.json()["events"]
                                 if event["kind"] == "model_decision"]
                    controls = [{key: item.get(key) for key in ("index", "tag", "text", "row_label", "label")}
                                for item in state_response.json()["intervention"]["observation"]["controls"]]
                    raise RuntimeError(f"model discovery stopped: {started['result']['observed']}; "
                                       f"run_id={started['run_id']}; decisions={decisions}; controls={controls}")
                capability_id = started["result"]["capability_id"]
                discovery_trace = {"run_id": started["run_id"], "status": "success",
                                   "steps": [{"kind": event["kind"],
                                              "step_id": event["detail"].get("step_id"),
                                              "action": event["detail"].get("action")}
                                             for event in state_response.json()["events"]]}
        else:
            browser = BrowserSurface(PLAYWRIGHT_URL, POLICY.check_url)
            run_id = "shared-discovery-demo"
            try:
                await browser.start()
                await browser.set_demo_identity(request.target_url, issue_token(
                    actor, audience="bank-gpt-browser", tenant_id=1, run_id=run_id, ttl=3600))
                await browser.navigate(request.target_url)
                session = RunSession(id=run_id, mode="discovery", browser=browser, policy=POLICY,
                                     values=request.values, actor_id=actor.id, tenant_id=1, request=request)
                discovered = await discover(session, ObservedFlow(), ARTIFACT_DIR)
                assert discovered.status == "success", discovered
            finally:
                await browser.close()
            assert discovered.capability_id
            capability_id = discovered.capability_id
    store = ArtifactStore(ARTIFACT_DIR)
    base = store.base(args.existing or capability_id)
    north = store.binding(base.id, 1)
    pine = pine_binding(base.id, north)
    async with httpx.AsyncClient(base_url=API, timeout=45) as client:
        async def login(username, password):
            response = await client.post("/auth/token", json={"username": username, "password": password})
            response.raise_for_status()
            return {"Authorization": "Bearer " + response.json()["access_token"]}
        operator = await login("pine_operator", "pine-operator-pass")
        north_operator = await login("north_operator", "north-operator-pass")
        reader = await login("shared_reader", "shared-demo-pass")
        if discovery_trace:
            close_discovery = await client.delete(f"/runs/{discovery_trace['run_id']}",
                                                  headers=north_operator)
            close_discovery.raise_for_status()
        if not args.existing:
            response = await client.post(f"/capabilities/{base.id}/bindings", headers=operator,
                                         json=pine.model_dump(mode="json"))
            response.raise_for_status()
            for tenant_id, operator_headers in [(1, north_operator), (2, operator)]:
                evaluation_ids = []
                for index in range(1, 4):
                    grant = await client.post(f"/capabilities/{base.id}/grants",
                                              headers=operator_headers,
                                              json={"tenant_id": tenant_id, "operation": "evaluate"})
                    grant.raise_for_status()
                    evaluation_headers = operator_headers | {
                        "X-Secure-Permissions": grant.json()["secure_permissions"]}
                    evaluated = await client.post(f"/capabilities/{base.id}/replay",
                                                  headers=evaluation_headers,
                                                  json={"tenant_id": tenant_id,
                                                        "values": {"member_id": str(tenant_id * 10000 + index)},
                                                        "evaluation": True})
                    evaluated.raise_for_status()
                    assert evaluated.json()["result"]["status"] == "success", evaluated.json()
                    evaluation_ids.append(evaluated.json()["run_id"])
                stability = await client.post(f"/capabilities/{base.id}/stability",
                                              headers=operator_headers,
                                              json={"tenant_id": tenant_id, "run_ids": evaluation_ids})
                stability.raise_for_status()
                assert stability.json()["rate"] == 1.0
                approval = await client.post(f"/capabilities/{base.id}/bindings/{tenant_id}/approve",
                                             headers=operator_headers)
                approval.raise_for_status()
                assert approval.json()["approval_state"] == "approved"
                for evaluation_id in evaluation_ids:
                    closed = await client.delete(f"/runs/{evaluation_id}",
                                                 headers=operator_headers)
                    closed.raise_for_status()
        runs = []
        for tenant_id, member, expected in [(1, "10001", "success"), (2, "20001", "success"),
                                            (1, "20001", "business_outcome"),
                                            (2, "10001", "business_outcome")]:
            grant_response = await client.post(f"/capabilities/{base.id}/grants",
                                               headers=reader, json={"tenant_id": tenant_id})
            grant_response.raise_for_status()
            headers = reader | {"X-Secure-Permissions": grant_response.json()["secure_permissions"]}
            replay_response = await client.post(f"/capabilities/{base.id}/replay", headers=headers,
                                                json={"tenant_id": tenant_id,
                                                      "values": {"member_id": member}})
            replay_response.raise_for_status()
            started = replay_response.json()
            assert started["result"]["status"] == expected, started
            if expected == "success":
                assert "balance" in started["result"]["outputs"]
            else:
                assert started["result"]["code"] == "member_not_found"
            state_response = await client.get(f"/runs/{started['run_id']}", headers=reader)
            state_response.raise_for_status()
            state = state_response.json()
            assert expected != "success" or any(e["kind"] == "checkpoint_verified" for e in state["events"])
            runs.append({"tenant_id": tenant_id, "member_id": "[input]", "run_id": started["run_id"],
                         "status": expected, "code": started["result"]["code"],
                         "output_names": list(started["result"]["outputs"]),
                         "checkpoint_verified": any(e["kind"] == "checkpoint_verified" for e in state["events"])})
            close = await client.delete(f"/runs/{started['run_id']}",
                                        headers=operator if tenant_id == 2 else north_operator)
            close.raise_for_status()
    drifted_targets = dict(pine.targets)
    drifted_targets["member_input"] = Target(description="Outdated customer reference",
        candidates=[Locator(strategy="name", value="old_customer_ref")])
    drifted = pine.model_copy(update={"targets": drifted_targets})
    drift_browser = BrowserSurface(PLAYWRIGHT_URL, POLICY.check_url)
    try:
        await drift_browser.start()
        await drift_browser.set_demo_identity(pine.entry_url, issue_token(
            actor, audience="bank-gpt-browser", tenant_id=2, run_id="binding-drift-demo", ttl=3600))
        await drift_browser.navigate(pine.entry_url)
        drift_session = RunSession(id="binding-drift-demo", mode="replay", browser=drift_browser,
                                   policy=POLICY, values={"member_id": "20001"},
                                   actor_id=actor.id, tenant_id=2, capability=drifted.resolve(base))
        drift_result = await replay(drift_session)
        assert (drift_result.status, drift_result.code) == ("intervention_required", "target_unavailable")
        assert not any(e.kind == "step_completed" for e in drift_session.events)
    finally:
        await drift_browser.close()
    report = {"base_capability_id": base.id, "schema_version": base.schema_version,
              "vendor_product": base.vendor_product, "vendor_version": base.vendor_version,
              "decision_source": "existing approved artifact" if args.existing else
                                 "configured model" if args.model_discover else
                                 "observed deterministic test decisions",
              "discovery": discovery_trace,
              "remote_playwright": PLAYWRIGHT_URL, "binding_tenants": [1, 2],
              "logical_controls": sorted(base.controls), "runs": runs,
              "tenant_2_drift": {"status": drift_result.status, "code": drift_result.code,
                                 "affected_control": "member_input", "binding_persisted": False,
                                 "tenant_1_run_succeeded": runs[0]["status"] == "success"},
              "assertion": "passed"}
    published_report = public_data(report)
    args.output.write_text(json.dumps(published_report, indent=2) + "\n")
    print(json.dumps(published_report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
