"""FastMCP stdio adapter. All data and authorization flow through the HTTP gateway."""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Bank GPT", instructions="List approved capabilities before invoking one by name.")


def identity_token() -> str:
    token = os.getenv("BANKGPT_IDENTITY_TOKEN", "")
    if not token:
        raise ValueError("BANKGPT_IDENTITY_TOKEN is required for the stdio host")
    return token


async def gateway(method: str, path: str, identity_token: str,
                  permission_token: str | None = None,
                  payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = os.getenv("BANKGPT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"Authorization": f"Bearer {identity_token}"}
    if permission_token:
        headers["X-Secure-Permissions"] = permission_token
    async with httpx.AsyncClient(base_url=base, timeout=90) as client:
        response = await client.request(method, path, headers=headers, json=payload)
    if response.is_error:
        try:
            detail = response.json().get("detail", "request denied")
        except ValueError:
            detail = "request denied"
        raise ValueError(f"gateway HTTP {response.status_code}: {detail}")
    return response.json()


@mcp.tool
async def capability_catalog(tenant_id: int) -> dict[str, Any]:
    """List approved capabilities and typed contracts for a tenant."""
    return await gateway("GET", f"/capabilities/catalog?tenant_id={tenant_id}", identity_token())


@mcp.tool
async def invoke_capability(tenant_id: int, name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Invoke an approved capability by qualified name with a fresh scoped grant."""
    token = identity_token()
    capability_id = name.rsplit("@", 1)[-1]
    grant = await gateway("POST", f"/capabilities/{capability_id}/grants", token,
                          payload={"tenant_id": tenant_id, "operation": "replay"})
    return await gateway("POST", "/capabilities/invoke", token, grant["secure_permissions"],
                         {"tenant_id": tenant_id, "name": name, "values": values})


@mcp.tool
async def run_status(run_id: str) -> dict[str, Any]:
    """Read the caller's run status and scrubbed events."""
    return await gateway("GET", f"/runs/{run_id}", identity_token())


if __name__ == "__main__":
    mcp.run(transport="stdio")
