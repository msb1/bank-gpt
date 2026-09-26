"""FastAPI boundary and minimal same-session operator console."""
from __future__ import annotations

import os
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import httpx
from playwright.async_api import Error as PlaywrightError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .artifacts import ArtifactError, ArtifactStore
from .core import ModelClient, Policy, RunSession, discover, origin, replay, target_from_control
from .driver import BrowserSurface, DriverError
from .models import BaseCapability, DiscoveryRequest, InputRef, ReplayRequest, RunResult, Step, TenantBinding, validate_values
from .scenarios import SCENARIOS


def load_env() -> None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()
from .demo import router as demo_router
from .tenant import Actor, GATEWAY, issue_token, verify_token

ARTIFACT_DIR = Path(os.getenv("BANKGPT_ARTIFACT_DIR", str(Path(__file__).resolve().parents[2] / "artifacts")))
STORE = ArtifactStore(ARTIFACT_DIR)
PLAYWRIGHT_URL = os.getenv("BANKGPT_PLAYWRIGHT_WS_URL", "ws://127.0.0.1:8080/")
POLICY = Policy(os.getenv("BANKGPT_ALLOWED_ORIGINS", "http://127.0.0.1:8000").split(","),
                os.getenv("BANKGPT_ALLOWED_ROUTES", "/demo,/demo/pine,/demo/search,/demo/result,/demo/verify").split(","),
                os.getenv("BANKGPT_ALLOWED_ACTIONS", "click,type,extract").split(","))
MODEL = ModelClient(os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1"),
                    os.getenv("OPENAI_API_KEY", ""), os.getenv("MAIN_MODEL", "qwen2.5-7b-instruct-mlx"))
RUNS: dict[str, RunSession] = {}

app = FastAPI(title="Bank GPT Computer Use", version="0.1.0")
app.include_router(demo_router)


class StartedRun(BaseModel):
    run_id: str
    result: RunResult


class LoginRequest(BaseModel):
    username: str
    password: str


class OperatorAction(BaseModel):
    action: Literal["click", "type"]
    index: int
    value: str | None = None
    parameter: str | None = None


class ResumeRequest(BaseModel):
    completed_step: bool = False


class GrantRequest(BaseModel):
    tenant_id: int
    operation: Literal["replay", "evaluate"] = "replay"


class InvokeRequest(BaseModel):
    tenant_id: int = Field(gt=0)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*@[0-9a-f-]{36}$")
    values: dict[str, Any]


class StabilityRequest(BaseModel):
    tenant_id: int = Field(gt=0)
    run_ids: list[str] = Field(min_length=1)


def catalog_for(actor: Actor, tenant_id: int) -> list[dict[str, Any]]:
    try:
        GATEWAY.require(actor, tenant_id, "capability:run")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return [{"id": base.id, "name": base.name, "invoke_name": f"{base.name}@{base.id}",
             "description": base.description,
             "artifact_version": base.schema_version, "binding_version": binding.version,
             "inputs": [item.model_dump(mode="json") for item in base.inputs],
             "outputs": [item.model_dump(mode="json") for item in base.outputs]}
            for base, binding in STORE.catalog(tenant_id)]


@app.get("/capabilities/catalog")
async def capability_catalog(tenant_id: int,
                             authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "capabilities": catalog_for(caller_auth(authorization), tenant_id)}


@app.post("/capabilities/invoke", response_model=StartedRun)
async def invoke_by_name(request: InvokeRequest,
                         authorization: str | None = Header(default=None),
                         x_secure_permissions: str | None = Header(default=None)) -> StartedRun:
    actor = caller_auth(authorization)
    matches = [item for item in catalog_for(actor, request.tenant_id)
               if item["invoke_name"] == request.name]
    if not matches:
        raise HTTPException(404, "approved capability name not found")
    if len(matches) != 1:
        raise HTTPException(409, "capability name is ambiguous")
    return await start_replay(matches[0]["id"], ReplayRequest(tenant_id=request.tenant_id,
                              values=request.values), authorization, x_secure_permissions)


def run_or_404(run_id: str) -> RunSession:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(404, "run not found")
    return session


def caller_auth(authorization: str | None) -> Actor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "bearer access token required")
    try:
        actor, _ = verify_token(authorization[7:], "bank-gpt-api")
        return actor
    except PermissionError as error:
        raise HTTPException(401, str(error)) from error


def run_auth(actor: Actor, session: RunSession, permission: str = "capability:run") -> None:
    try:
        GATEWAY.require(actor, session.tenant_id, permission)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    if permission != "run:operate" and actor.id != session.actor_id:
        raise HTTPException(403, "run belongs to another actor")


@app.post("/auth/token")
async def demo_token(request: LoginRequest) -> dict[str, str | int]:
    actor = GATEWAY.authenticate(request.username, request.password)
    if actor is None:
        raise HTTPException(401, "invalid demo credentials")
    return {"access_token": issue_token(actor, audience="bank-gpt-api"),
            "token_type": "bearer", "expires_in": 900}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.post("/discover", response_model=StartedRun)
async def start_discovery(request: DiscoveryRequest,
                          authorization: str | None = Header(default=None)) -> StartedRun:
    actor = caller_auth(authorization)
    try:
        GATEWAY.require(actor, request.tenant_id, "capability:run")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    try:
        validate_values(request.inputs, request.values)
        POLICY.check_url(request.target_url)
        if len({item.name for item in request.inputs}) != len(request.inputs):
            raise ValueError("duplicate input name")
        if any(isinstance(value, str) and len(value) >= 4 and value in request.goal
               for value in request.values.values()):
            raise ValueError("goal must be reusable and must not contain a literal input value")
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    browser = BrowserSurface(PLAYWRIGHT_URL, POLICY.check_url)
    run_id = str(uuid4())
    try:
        await browser.start()
        await browser.set_demo_identity(request.target_url, issue_token(
            actor, audience="bank-gpt-browser", tenant_id=request.tenant_id, run_id=run_id, ttl=3600))
        await browser.navigate(request.target_url)
    except (DriverError, PlaywrightError, httpx.HTTPError) as error:
        await browser.close()
        raise HTTPException(502, f"Playwright unavailable: {error}") from error
    session = RunSession(id=run_id, mode="discovery", browser=browser,
                         policy=POLICY, values=request.values.copy(), actor_id=actor.id,
                         tenant_id=request.tenant_id, request=request)
    RUNS[session.id] = session
    session.log("discovery_started", goal=request.goal, target=origin(request.target_url),
                input_names=list(request.values))
    try:
        result = await asyncio.wait_for(discover(session, MODEL, ARTIFACT_DIR), request.timeout_seconds)
    except TimeoutError:
        result = await session.handoff("discovery deadline reached")
    return StartedRun(run_id=session.id, result=result)


def read_capability(capability_id: str) -> BaseCapability:
    try:
        return STORE.base(capability_id)
    except ArtifactError as error:
        raise HTTPException(422 if "unsupported artifact schema" in str(error) else 404,
                            str(error)) from error


def approved_binding(capability_id: str, tenant_id: int) -> TenantBinding:
    try:
        binding = STORE.binding(capability_id, tenant_id)
    except ArtifactError as error:
        raise HTTPException(404, str(error)) from error
    if binding.approval_state != "approved":
        raise HTTPException(403, "tenant binding is not approved")
    return binding


@app.get("/capabilities/{capability_id}", response_model=BaseCapability)
async def capability(capability_id: str, tenant_id: int,
                     authorization: str | None = Header(default=None)) -> BaseCapability:
    actor = caller_auth(authorization)
    cap = read_capability(capability_id)
    try:
        GATEWAY.require(actor, tenant_id, "capability:run")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    approved_binding(capability_id, tenant_id)
    return cap


@app.post("/capabilities/{capability_id}/bindings", response_model=TenantBinding)
async def add_binding(capability_id: str, binding: TenantBinding,
                      authorization: str | None = Header(default=None)) -> TenantBinding:
    actor = caller_auth(authorization)
    base = read_capability(capability_id)
    try:
        GATEWAY.require(actor, binding.tenant_id, "run:operate")
        if binding.base_id != base.id:
            raise ValueError("binding base ID mismatch")
        if (binding.version != 1 or binding.approval_state != "draft" or binding.reviewed_by is not None or
                binding.reviewed_at is not None or binding.stability is not None):
            raise ValueError("new binding must be an unreviewed draft")
        if binding.allowed_origin != origin(binding.entry_url):
            raise ValueError("binding origin mismatch")
        POLICY.check_url(binding.entry_url)
        binding.resolve(base)
        reviewed = binding.model_copy(update={"reviewed_by": actor.id,
                                               "reviewed_at": datetime.now(timezone.utc)})
        STORE.save_binding(reviewed)
        return reviewed
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except FileExistsError as error:
        raise HTTPException(409, "binding already exists") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.post("/capabilities/{capability_id}/bindings/{tenant_id}/approve", response_model=TenantBinding)
async def approve_binding(capability_id: str, tenant_id: int,
                          authorization: str | None = Header(default=None)) -> TenantBinding:
    actor = caller_auth(authorization)
    base = read_capability(capability_id)
    try:
        GATEWAY.require(actor, tenant_id, "run:operate")
        binding = STORE.binding(capability_id, tenant_id)
        POLICY.check_url(binding.entry_url)
        binding.resolve(base)
        if binding.approval_state != "draft":
            raise ValueError("only draft bindings can be approved")
        if (not binding.stability or binding.stability.get("rate") is None or
                binding.stability.get("binding_version") != binding.version - 1 or
                len(binding.stability.get("runs", [])) < 3 or
                binding.stability["rate"] < 0.9):
            raise ValueError("stability results are required for approval")
        reviewed = binding.model_copy(update={"approval_state": "approved", "version": binding.version + 1,
                                               "reviewed_by": actor.id,
                                               "reviewed_at": datetime.now(timezone.utc)})
        STORE.review_binding(reviewed)
        return reviewed
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ArtifactError, ValueError) as error:
        raise HTTPException(422, str(error)) from error


@app.post("/capabilities/{capability_id}/bindings/{tenant_id}/revoke", response_model=TenantBinding)
async def revoke_binding(capability_id: str, tenant_id: int,
                         authorization: str | None = Header(default=None)) -> TenantBinding:
    actor = caller_auth(authorization)
    try:
        GATEWAY.require(actor, tenant_id, "run:operate")
        binding = STORE.binding(capability_id, tenant_id)
        if binding.approval_state != "approved":
            raise ValueError("only approved bindings can be revoked")
        revoked = binding.model_copy(update={"version": binding.version + 1,
                                             "approval_state": "revoked",
                                             "reviewed_by": actor.id,
                                             "reviewed_at": datetime.now(timezone.utc)})
        STORE.review_binding(revoked)
        return revoked
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ArtifactError, ValueError) as error:
        raise HTTPException(422, str(error)) from error


@app.post("/capabilities/{capability_id}/bindings/{tenant_id}/revisions", response_model=TenantBinding)
async def revise_binding(capability_id: str, tenant_id: int, proposal: TenantBinding,
                         authorization: str | None = Header(default=None)) -> TenantBinding:
    actor = caller_auth(authorization)
    base = read_capability(capability_id)
    try:
        GATEWAY.require(actor, tenant_id, "run:operate")
        previous = STORE.binding(capability_id, tenant_id)
        if (proposal.base_id != base.id or proposal.tenant_id != tenant_id or
                proposal.approval_state != "draft" or proposal.stability is not None):
            raise ValueError("revision must be a draft of this tenant binding")
        POLICY.check_url(proposal.entry_url)
        if proposal.allowed_origin != origin(proposal.entry_url):
            raise ValueError("binding origin mismatch")
        proposal.resolve(base)
        revised = proposal.model_copy(update={"version": previous.version + 1,
                                              "reviewed_by": None, "reviewed_at": None})
        STORE.review_binding(revised)
        return revised
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ArtifactError, ValueError) as error:
        raise HTTPException(422, str(error)) from error


@app.post("/capabilities/{capability_id}/stability")
async def stability_report(capability_id: str, request: StabilityRequest,
                           authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = caller_auth(authorization)
    try:
        GATEWAY.require(actor, request.tenant_id, "run:operate")
        binding = STORE.binding(capability_id, request.tenant_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ArtifactError as error:
        raise HTTPException(404, str(error)) from error
    if len(set(request.run_ids)) != len(request.run_ids):
        raise HTTPException(422, "duplicate run IDs")
    counts = {key: 0 for key in ("success", "business_outcome", "intervention_required", "failure")}
    references = []
    for run_id in request.run_ids:
        session = run_or_404(run_id)
        if (session.tenant_id != request.tenant_id or not session.capability or
                session.binding_version != binding.version or
                session.capability.id != capability_id or session.result is None):
            raise HTTPException(422, "run does not match binding or has no result")
        counts[session.result.status] += 1
        references.append({"run_id": run_id, "status": session.result.status,
                           "code": session.result.code})
    report = {"capability_id": capability_id, "artifact_version": read_capability(capability_id).schema_version,
              "tenant_id": request.tenant_id,
              "binding_version": binding.version, "runs": references, "counts": counts,
              "rate": counts["success"] / len(references)}
    if binding.approval_state == "draft":
        STORE.review_binding(binding.model_copy(update={"version": binding.version + 1,
                                                  "stability": report}))
    return report


@app.post("/capabilities/{capability_id}/grants")
async def capability_grant(capability_id: str, request: GrantRequest,
                           authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = caller_auth(authorization)
    read_capability(capability_id)
    try:
        GATEWAY.require(actor, request.tenant_id, "capability:run")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    if request.operation == "evaluate":
        try:
            GATEWAY.require(actor, request.tenant_id, "run:operate")
            binding = STORE.binding(capability_id, request.tenant_id)
            if binding.approval_state != "draft":
                raise PermissionError("evaluation is only for draft bindings")
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ArtifactError as error:
            raise HTTPException(404, str(error)) from error
    else:
        approved_binding(capability_id, request.tenant_id)
    grant = GATEWAY.issue_permission(actor, request.tenant_id, capability_id, request.operation)
    return {"secure_permissions": grant, "token_type": "capability", "expires_in": 120}


@app.post("/capabilities/{capability_id}/replay", response_model=StartedRun)
async def start_replay(capability_id: str, request: ReplayRequest,
                       authorization: str | None = Header(default=None),
                       x_secure_permissions: str | None = Header(default=None)) -> StartedRun:
    actor = caller_auth(authorization)
    base = read_capability(capability_id)
    if not x_secure_permissions:
        raise HTTPException(401, "X-Secure-Permissions capability grant required")
    try:
        grant_actor, grant = verify_token(x_secure_permissions, "bank-gpt-capability")
        if (grant_actor.id != actor.id or grant["tenant_id"] != request.tenant_id or
                grant["capability_id"] != base.id or
                grant["operation"] != ("evaluate" if request.evaluation else "replay")):
            raise PermissionError("capability grant scope mismatch")
        GATEWAY.require(actor, request.tenant_id, "capability:run")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    if request.evaluation:
        try:
            GATEWAY.require(actor, request.tenant_id, "run:operate")
            binding = STORE.binding(capability_id, request.tenant_id)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ArtifactError as error:
            raise HTTPException(404, str(error)) from error
        if binding.approval_state != "draft":
            raise HTTPException(403, "evaluation is only for draft bindings")
    else:
        binding = approved_binding(capability_id, request.tenant_id)
    if request.expected_binding_version is not None and binding.version != request.expected_binding_version:
        raise HTTPException(409, "tenant binding version changed")
    try:
        cap = binding.resolve(base)
        validate_values(base.inputs, request.values)
        POLICY.check_url(cap.entry_url)
        if cap.allowed_origin != origin(cap.entry_url):
            raise ValueError("artifact origin mismatch")
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if request.scenario:
        try:
            GATEWAY.require(actor, request.tenant_id, "run:operate")
        except PermissionError as error:
            raise HTTPException(403, "scenario requires tenant operator") from error
    if request.assisted_fallback:
        try:
            GATEWAY.require(actor, request.tenant_id, "run:operate")
        except PermissionError as error:
            raise HTTPException(403, "assisted fallback requires tenant operator") from error
    run_id = str(uuid4())
    if request.scenario:
        SCENARIOS.attach(run_id, actor.id, request.tenant_id, request.scenario)
    browser = BrowserSurface(PLAYWRIGHT_URL, POLICY.check_url)
    try:
        await browser.start()
        await browser.set_demo_identity(cap.entry_url, issue_token(
            actor, audience="bank-gpt-browser", tenant_id=request.tenant_id, run_id=run_id, ttl=3600))
        await browser.navigate(cap.entry_url)
        observed = await browser.observe()
        if (observed["title"] != binding.ui_fingerprint or
                observed["ui_version"] != binding.ui_version):
            raise ValueError("tenant UI fingerprint mismatch")
    except ValueError as error:
        await browser.close()
        SCENARIOS.discard(run_id)
        raise HTTPException(409, str(error)) from error
    except (DriverError, PlaywrightError, httpx.HTTPError) as error:
        await browser.close()
        SCENARIOS.discard(run_id)
        raise HTTPException(502, f"Playwright unavailable: {error}") from error
    session = RunSession(id=run_id, mode="replay", browser=browser,
                         policy=POLICY, values=request.values.copy(), actor_id=actor.id,
                         tenant_id=request.tenant_id, capability=cap,
                         assisted_fallback=request.assisted_fallback,
                         binding_version=binding.version)
    RUNS[session.id] = session
    session.log("replay_started", capability_id=cap.id, input_names=list(request.values))
    if request.scenario:
        session.log("scenario_attached", fault=request.scenario.fault,
                    step_id=request.scenario.step_id, occurrences=request.scenario.occurrences,
                    delay_seconds=request.scenario.delay_seconds)
    result = await replay(session, MODEL)
    session.result = result
    return StartedRun(run_id=session.id, result=result)


@app.get("/runs/{run_id}")
async def run_status(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = caller_auth(authorization)
    session = run_or_404(run_id)
    run_auth(actor, session)
    return {"run_id": session.id, "mode": session.mode, "owner": session.owner,
            "binding_version": session.binding_version,
            "result": session.result.model_dump(mode="json") if session.result else None,
            "browser_session_id": session.browser_session_id,
            "next_step": session.next_step,
            "current_step": (session.capability.steps[session.next_step].id
                             if session.capability and session.next_step < len(session.capability.steps)
                             else None),
            "intervention": session.intervention.model_dump(mode="json") if session.intervention else None,
            "events": [event.model_dump(mode="json") for event in session.events]}


@app.get("/runs/{run_id}/operator", response_class=HTMLResponse)
async def operator_page(run_id: str) -> str:
    return """<!doctype html><html><head><title>Bank GPT Operator</title></head><body>
<h1>Operator handoff</h1><p>Enter an operator access token and inspect the live browser controls.</p>
<label>Token <input id="token" type="password"></label><button onclick="refresh()">Refresh</button>
<pre id="status"></pre><div id="controls"></div>
<label><input id="completed" type="checkbox">I performed the paused recorded step</label>
<button onclick="resume()">Resume automation</button>
<script>
const run = location.pathname.split('/')[2];
const headers = () => ({'Content-Type':'application/json','Authorization':'Bearer '+document.getElementById('token').value});
async function refresh() {
  const r=await fetch(`/runs/${run}/operator/state`,{headers:headers()});
  const data=await r.json(); document.getElementById('status').textContent=JSON.stringify(data.intervention,null,2);
  const root=document.getElementById('controls'); root.replaceChildren();
  for(const c of (data.observation?.controls||[])) {
    const row=document.createElement('p'); row.textContent=`${c.index}: ${c.tag} ${c.label||c.text||c.name||''} `;
    const click=document.createElement('button'); click.textContent='Click';
    click.onclick=()=>act('click',c.index); row.append(click);
    const input=document.createElement('input'); input.placeholder='Type value'; row.append(input);
    const type=document.createElement('button'); type.textContent='Type';
    type.onclick=()=>act('type',c.index,input.value,prompt('Input parameter name (discovery only)'));
    row.append(type); root.append(row);
  }
}
async function act(action,index,value,parameter) {
  const r=await fetch(`/runs/${run}/operator/action`,{method:'POST',headers:headers(),
    body:JSON.stringify({action,index,value,parameter})}); alert(JSON.stringify(await r.json())); refresh();
}
async function resume() {
  const completed_step=document.getElementById('completed').checked;
  const r=await fetch(`/runs/${run}/resume`,{method:'POST',headers:headers(),body:JSON.stringify({completed_step})});
  alert(JSON.stringify(await r.json())); refresh();
}
</script></body></html>"""


@app.get("/runs/{run_id}/operator/state")
async def operator_state(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = caller_auth(authorization)
    session = run_or_404(run_id)
    run_auth(actor, session, "run:operate")
    if session.owner != "human":
        raise HTTPException(409, "automation owns session")
    return {"browser_session_id": session.browser_session_id,
            "current_step": session.intervention.step_id if session.intervention else None,
            "owner": session.owner,
            "intervention": session.intervention.model_dump(mode="json") if session.intervention else None,
            "observation": await session.observation()}


@app.post("/runs/{run_id}/operator/action")
async def operator_action(run_id: str, request: OperatorAction,
                          authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = caller_auth(authorization)
    session = run_or_404(run_id)
    run_auth(actor, session, "run:operate")
    async with session.lock:
        if session.owner != "human":
            raise HTTPException(409, "automation owns session")
        observation = await session.observation()
        try:
            if request.index < 0:
                raise ValueError("control index must be non-negative")
            control = observation["controls"][request.index]
            if request.action not in session.policy.actions:
                raise ValueError("action is outside the configured allowlist")
            target = target_from_control(control)
            completed_recorded_action = False
            if (session.mode == "replay" and session.intervention and session.capability
                    and session.next_step < len(session.capability.steps)):
                step = session.capability.steps[session.next_step]
                if (session.intervention.step_id == step.id and request.action == step.action):
                    completed_recorded_action = await session.browser.same_target(target, step.target)
            if request.action == "click":
                if control.get("destination"):
                    session.policy.check_url(control["destination"])
                chosen = await session.browser.click(target)
            else:
                if request.value is None:
                    raise ValueError("type requires value")
                if session.mode == "discovery":
                    if request.parameter not in {p.name for p in session.request.inputs}:
                        raise ValueError("discovery operator type requires declared parameter")
                    if session.request.values[request.parameter] != request.value:
                        raise ValueError("operator value must match declared input for reusable recording")
                session.values[f"_operator_{len(session.events)}"] = request.value
                chosen = await session.browser.type(target, request.value)
            if session.mode == "discovery":
                session.steps.append(Step(id=f"step_{len(session.steps)+1}", action=request.action,
                                          target=target,
                                          value=InputRef(parameter=request.parameter) if request.action == "type" else None,
                                          risk="risky" if request.action == "click" and session.policy.is_risky(control) else "safe"))
            await session.check_browser()
            if completed_recorded_action:
                session.intervention.completed_step_action = True
            session.log("human_action", operator_id=actor.id, action=request.action, target=target.description,
                        locator=chosen.model_dump(), browser_session_id=session.browser_session_id)
            return {"status": "ok", "observation": await session.observation()}
        except (IndexError, ValueError, DriverError, httpx.HTTPError, PlaywrightError) as error:
            raise HTTPException(422, str(error)) from error


@app.post("/runs/{run_id}/resume", response_model=StartedRun)
async def resume_run(run_id: str, request: ResumeRequest,
                     authorization: str | None = Header(default=None)) -> StartedRun:
    actor = caller_auth(authorization)
    session = run_or_404(run_id)
    run_auth(actor, session, "run:operate")
    async with session.lock:
        if session.owner != "human":
            raise HTTPException(409, "session is not with human operator")
        if request.completed_step and session.mode != "replay":
            raise HTTPException(422, "completed_step applies only to replay")
        if request.completed_step and (not session.intervention or
                                       not session.intervention.completed_step_action):
            raise HTTPException(422, "paused step has no matching operator action")
        if request.completed_step:
            session.next_step += 1
        if session.intervention:
            session.intervention.state = "resolved"
            session.intervention.owner = "automation"
        session.owner = "automation"
        session.log("human_handoff_completed", operator_id=actor.id,
                    completed_step=request.completed_step, next_step=session.next_step,
                    browser_session_id=session.browser_session_id)
    result = await (discover(session, MODEL, ARTIFACT_DIR) if session.mode == "discovery" else replay(session, MODEL))
    session.result = result
    return StartedRun(run_id=session.id, result=result)


@app.delete("/runs/{run_id}")
async def close_run(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, str]:
    actor = caller_auth(authorization)
    session = run_or_404(run_id)
    run_auth(actor, session, "run:operate")
    await session.browser.close()
    SCENARIOS.discard(run_id)
    RUNS.pop(run_id, None)
    return {"status": "closed"}
