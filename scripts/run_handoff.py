"""Create a live verification handoff; optionally operate the console and save proof."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright

from bank_gpt.api import PLAYWRIGHT_URL, POLICY
from bank_gpt.publication import public_text

CAPABILITY_ID = "d847b7f4-6460-433f-b53c-4f4640c2d352"
MEMBER_ID = "10001"


def scrub(value: object) -> object:
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()
                if key not in {"access_token", "password"}}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        value = value.replace(MEMBER_ID, "[input]")
        value = re.sub(r"\$\s?\d[\d,]*\.\d\d", "[amount]", value)
        return public_text(re.sub(r"\b\d{5,}\b", "[number]", value))
    return value


async def login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/token", json={"username": username, "password": password})
    response.raise_for_status()
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def operate_ui(url: str, token: str, control_name: str,
                     resulting_text: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect(PLAYWRIGHT_URL)
        context = await browser.new_context()
        try:
            page = await context.new_page()
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
            await page.goto(url)
            await page.locator("#token").fill(token)
            await page.get_by_role("button", name="Refresh").click()
            row = page.locator("p").filter(has_text=control_name)
            await row.wait_for()
            async with page.expect_response(lambda response: "/operator/action" in response.url) as action_response:
                await row.get_by_role("button", name="Click").click()
            response = await action_response.value
            if response.status != 200:
                raise RuntimeError(f"operator page click failed: {response.status} {await response.text()}")
            await page.get_by_text(resulting_text).first.wait_for()
        finally:
            await context.close()
            await browser.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--capability", default=CAPABILITY_ID)
    parser.add_argument("--operator-origin", default=sorted(POLICY.allowed_origins)[0],
                        help="API origin reachable from the remote browser")
    parser.add_argument("--output", type=Path, default=Path("evidence/handoff"))
    parser.add_argument("--auto-operator", action="store_true",
                        help="exercise the operator page with Playwright and save the trace")
    parser.add_argument("--check-restart", metavar="RUN_ID",
                        help="after restarting the API, verify a paused process-local run is gone")
    args = parser.parse_args()
    async with httpx.AsyncClient(base_url=args.api, timeout=45) as client:
        operator = await login(client, "north_operator", "north-operator-pass")
        if args.check_restart:
            response = await client.get(f"/runs/{args.check_restart}", headers=operator)
            assert response.status_code == 404, response.status_code
            args.output.mkdir(parents=True, exist_ok=True)
            path = args.output / "restart-check.json"
            path.write_text(json.dumps({"run_id": args.check_restart,
                "status_after_restart": response.status_code,
                "detail": response.json().get("detail"),
                "assertion": "passed", "limitation": "active runs are process-local"}, indent=2) + "\n")
            print(f"restart check passed; trace={path}")
            return
        grant_response = await client.post(f"/capabilities/{args.capability}/grants",
            headers=operator, json={"tenant_id": 1})
        grant_response.raise_for_status()
        operator["X-Secure-Permissions"] = grant_response.json()["secure_permissions"]
        started_response = await client.post(f"/capabilities/{args.capability}/replay",
            headers=operator, json={"tenant_id": 1, "values": {"member_id": MEMBER_ID},
                                    "scenario": {"fault": "verification_required", "step_id": "step_3"}})
        started_response.raise_for_status()
        started = started_response.json()
        run_id = started["run_id"]
        operator_url = urljoin(args.operator_origin, f"/runs/{run_id}/operator")
        print(f"scenario_id={run_id}\noperator_url={operator_url}", flush=True)
        assert (started["result"]["status"], started["result"]["code"],
                started["result"]["step_id"]) == ("intervention_required", "verification_required", "step_3")
        if not args.auto_operator:
            paused_response = await client.get(f"/runs/{run_id}", headers=operator)
            paused_response.raise_for_status()
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "restart-paused.json").write_text(json.dumps(scrub({
                "scenario_id": run_id, "operator_url": operator_url,
                "started": started, "paused": paused_response.json(),
                "browser": "remote-playwright",
            }), indent=2) + "\n")
            print("Use the operator page to click Verify lookup, then resume. Close the run when done.")
            return
        try:
            state_response = await client.get(f"/runs/{run_id}", headers=operator)
            state_response.raise_for_status()
            paused = state_response.json()
            assert paused["owner"] == "human" and paused["current_step"] == "step_3"
            assert paused["intervention"]["observation"]["controls"]
            session_id = paused["browser_session_id"]
            denials = []
            for username, password in (("north_reader", "north-demo-pass"),
                                       ("cedar_operator", "cedar-demo-pass")):
                headers = await login(client, username, password)
                for path, payload in (("operator/action", {"action": "click", "index": 0}),
                                      ("resume", {"completed_step": False})):
                    response = await client.post(f"/runs/{run_id}/{path}", headers=headers, json=payload)
                    assert response.status_code == 403, (username, path, response.status_code)
                    denials.append({"actor": username, "action": path, "status": response.status_code})
            premature = await client.post(f"/runs/{run_id}/resume", headers=operator,
                                          json={"completed_step": True})
            assert premature.status_code == 422
            retry_response = await client.post(f"/runs/{run_id}/resume", headers=operator,
                                               json={"completed_step": False})
            retry_response.raise_for_status()
            retry = retry_response.json()
            assert retry["result"]["status"] == "intervention_required"
            assert retry["result"]["code"] == "verification_required"
            assert retry["result"]["intervention_id"] != started["result"]["intervention_id"]
            token = operator["Authorization"].removeprefix("Bearer ")
            await operate_ui(operator_url, token, "Verify lookup", "Savings balance")
            resumed_response = await client.post(f"/runs/{run_id}/resume", headers=operator,
                                                 json={"completed_step": False})
            resumed_response.raise_for_status()
            resumed = resumed_response.json()
            assert resumed["result"]["status"] == "success"
            state_response = await client.get(f"/runs/{run_id}", headers=operator)
            state_response.raise_for_status()
            final = state_response.json()
            assert final["browser_session_id"] == session_id
            assert final["next_step"] == 3 and final["owner"] == "automation"
            assert any(event["kind"] == "human_action" and event["detail"]["action"] == "click"
                       for event in final["events"])
            args.output.mkdir(parents=True, exist_ok=True)
            trace = {"scenario_id": run_id, "operator_url": operator_url,
                     "browser_session_id": session_id, "denials": denials,
                     "premature_completion_status": premature.status_code,
                     "paused": paused, "retry": retry, "resumed": resumed, "final": final,
                     "assertion": "passed", "browser": "remote-playwright"}
            (args.output / "verification-trace.json").write_text(
                json.dumps(scrub(trace), indent=2) + "\n")
            print(f"success; trace={args.output / 'verification-trace.json'}")
        finally:
            close = await client.delete(f"/runs/{run_id}", headers=operator)
            close.raise_for_status()
        completed_response = await client.post(f"/capabilities/{args.capability}/replay",
            headers=operator, json={"tenant_id": 1, "values": {"member_id": MEMBER_ID},
                                    "scenario": {"fault": "risky_control", "step_id": "step_2"}})
        completed_response.raise_for_status()
        completed = completed_response.json()
        completed_run = completed["run_id"]
        assert completed["result"]["status"] == "intervention_required"
        assert completed["result"]["step_id"] == "step_2"
        try:
            before_response = await client.get(f"/runs/{completed_run}", headers=operator)
            before_response.raise_for_status()
            before = before_response.json()
            completed_url = urljoin(args.operator_origin, f"/runs/{completed_run}/operator")
            await operate_ui(completed_url, token, "Search", "Savings balance")
            after_action_response = await client.get(f"/runs/{completed_run}", headers=operator)
            after_action_response.raise_for_status()
            after_action = after_action_response.json()
            assert after_action["intervention"]["completed_step_action"] is True
            finish_response = await client.post(f"/runs/{completed_run}/resume", headers=operator,
                                                json={"completed_step": True})
            finish_response.raise_for_status()
            finish = finish_response.json()
            assert finish["result"]["status"] == "success"
            final_response = await client.get(f"/runs/{completed_run}", headers=operator)
            final_response.raise_for_status()
            final_state = final_response.json()
            assert final_state["browser_session_id"] == before["browser_session_id"]
            assert final_state["next_step"] == 3
            assert any(event["kind"] == "human_handoff_completed" and
                       event["detail"]["completed_step"] is True and
                       event["detail"]["next_step"] == 2 for event in final_state["events"])
            (args.output / "completed-step-trace.json").write_text(json.dumps(scrub({
                "scenario_id": completed_run, "operator_url": completed_url,
                "before": before, "after_action": after_action, "resumed": finish,
                "final": final_state, "assertion": "passed", "browser": "remote-playwright",
            }), indent=2) + "\n")
            print(f"completed step; trace={args.output / 'completed-step-trace.json'}")
        finally:
            close = await client.delete(f"/runs/{completed_run}", headers=operator)
            close.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())
