"""FastMCP stdio client demonstration against the live Bank GPT HTTP gateway."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("evidence/agent-mcp/live-invocation.json"))
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--member-id", default="10001")
    args = parser.parse_args()
    api = os.getenv("BANKGPT_API_URL", "http://127.0.0.1:8000")
    async with httpx.AsyncClient(base_url=api, timeout=90) as http:
        login = await http.post("/auth/token", json={"username": "north_reader",
                                                       "password": "north-demo-pass"})
        login.raise_for_status()
        identity = login.json()["access_token"]
        transport = StdioTransport(sys.executable, ["-m", "bank_gpt.mcp_server"],
                                   env={**os.environ, "BANKGPT_API_URL": api,
                                        "BANKGPT_IDENTITY_TOKEN": identity})
        async with Client(transport) as client:
            tools = await client.list_tools()
            denied = False
            try:
                await client.call_tool("capability_catalog", {"tenant_id": 2})
            except Exception as error:
                denied = "403" in str(error)
            if not denied:
                raise RuntimeError("MCP cross-tenant catalog request was not denied")
            catalog_result = await client.call_tool("capability_catalog", {
                "tenant_id": args.tenant_id})
            catalog = catalog_result.structured_content
            candidates = [item for item in catalog["capabilities"]
                          if item["name"] == "read_savings_balance"]
            if not candidates:
                raise RuntimeError("approved capability absent")
            selected = max(candidates, key=lambda item: item["id"])
            invoked = await client.call_tool("invoke_capability", {
                "tenant_id": args.tenant_id, "name": selected["invoke_name"],
                "values": {"member_id": args.member_id}})
            run = invoked.structured_content
            if run["result"]["status"] != "success":
                raise RuntimeError(f"invocation did not succeed: {run['result']['status']}")
            state = await client.call_tool("run_status", {"run_id": run["run_id"]})
            status = state.structured_content
            if not any(event["kind"] == "checkpoint_verified" for event in status["events"]):
                raise RuntimeError("checkpoint event absent")
            trace = {"transport": "FastMCP stdio", "tools": [tool.name for tool in tools],
                     "cross_tenant_catalog_denied": denied,
                     "catalog_tenant_id": args.tenant_id,
                     "catalog_names": [item["invoke_name"] for item in catalog["capabilities"]],
                     "selection": {"invoke_name": selected["invoke_name"],
                                   "reason": "approved savings balance capability with typed member_id input",
                                   "inputs": selected["inputs"], "outputs": selected["outputs"]},
                     "invocation": {"name": selected["invoke_name"], "arguments": {"member_id": "[input]"},
                                    "run_id": run["run_id"], "status": run["result"]["status"],
                                    "output_names": list(run["result"]["outputs"]),
                                    "output_values": {key: "[amount]" for key in run["result"]["outputs"]}},
                     "status": {"run_id": status["run_id"], "owner": status["owner"],
                                "checkpoint_verified": True}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(trace, indent=2) + "\n")
            operator_login = await http.post("/auth/token", json={
                "username": "north_operator", "password": "north-operator-pass"})
            operator_login.raise_for_status()
            closed = await http.delete(f"/runs/{run['run_id']}", headers={
                "Authorization": "Bearer " + operator_login.json()["access_token"]})
            closed.raise_for_status()
            print(json.dumps({"run_id": run["run_id"], "status": run["result"]["status"],
                              "evidence": str(args.output)}))


if __name__ == "__main__":
    asyncio.run(main())
