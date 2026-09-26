"""Policy, model decisions, artifact compilation, and deterministic replay."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from playwright.async_api import Error as PlaywrightError

from .driver import BrowserSurface, DriverError, xpath_literal
from .artifacts import ArtifactStore
from .models import (BaseCapability, Capability, Condition, DiscoveryRequest, Event, InputRef,
                     Intervention, Locator, Parameter, RunResult, Step, Target,
                     LogicalCondition, LogicalStep, TenantBinding, validate_values)


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid browser URL")
    return f"{parts.scheme}://{parts.netloc}"


class Policy:
    def __init__(self, allowed_origins: list[str], allowed_routes: list[str], actions: list[str]) -> None:
        self.allowed_origins = {origin(value) for value in allowed_origins}
        self.allowed_routes = set(allowed_routes)
        self.actions = set(actions)

    def check_url(self, url: str) -> None:
        if origin(url) not in self.allowed_origins:
            raise ValueError("URL is outside the configured allowlist")
        if urlsplit(url).path not in self.allowed_routes:
            raise ValueError("route is outside the configured allowlist")

    def check_action(self, action: str, control: dict[str, Any] | None = None) -> None:
        if action not in self.actions:
            raise ValueError(f"action {action} is disallowed")
        if action != "click" or not control:
            return
        if self.is_risky(control):
            raise PermissionError("risky action requires human intervention")
        destination = control.get("destination")
        if destination:
            self.check_url(destination)

    @staticmethod
    def is_risky(control: dict[str, Any]) -> bool:
        text = " ".join(str(control.get(key) or "") for key in ("text", "label", "name"))
        return bool(re.search(r"\b(transfer|delete|submit|pay|close account|confirm)\b", text, re.I))


def redacted(value: Any, secrets: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: redacted(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in secrets.values():
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[input]")
    text = re.sub(r"\$\s?\d[\d,]*\.\d\d", "[amount]", text)
    text = re.sub(r"\b\d{5,}\b", "[number]", text)
    return text


def target_from_control(control: dict[str, Any]) -> Target:
    tag = control["tag"]
    description = control.get("label") or control.get("row_label") or control.get("text") or control.get("name") or tag
    locators: list[Locator] = []
    if control.get("name"):
        locators.append(Locator(strategy="name", value=control["name"]))
    if control.get("label"):
        locators.append(Locator(strategy="label", value=control["label"]))
    if control.get("row_label"):
        label = xpath_literal(control["row_label"])
        locators.append(Locator(strategy="xpath", value=f"//tr[td[1][normalize-space(.)={label}]]/td[2]"))
    if control.get("id"):
        locators.append(Locator(strategy="id", value=control["id"]))
    if control.get("text") and control["text"] != "[value]":
        locators.append(Locator(strategy="text", value=control["text"], tag=tag))
    if not locators:
        raise ValueError("control has no robust locator")
    return Target(description=description, candidates=locators)


class ModelClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.url = base_url.rstrip("/")
        if not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        self.api_key = api_key
        self.model = model

    async def propose_locator(self, observation: dict[str, Any], description: str,
                              action: str) -> int:
        payload = {"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": "Select exactly one visible control matching the requested logical target and action. Reply only as JSON: {\"index\": integer}. If uncertain, reply {\"index\": -1}."},
            {"role": "user", "content": json.dumps({"target": description, "action": action,
                                                    "observation": observation})}]}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(self.url, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        return int(json.loads(content)["index"])

    async def decide(self, goal: str, observation: dict[str, Any], inputs: list[Parameter],
                     outputs: list[Parameter], completed: list[str],
                     recent_steps: list[Step]) -> dict[str, Any]:
        system = ("You operate a browser UI. Reply with exactly one complete JSON object. "
                  "Choose action click, type, extract, finish, or stuck. Required shapes: "
                  "{\"action\":\"type\",\"index\":3,\"parameter\":\"member_id\",\"reason\":\"...\"}; "
                  "{\"action\":\"click\",\"index\":4,\"reason\":\"...\"}; "
                  "extract requires action, index from the CURRENT observation, output name, reason; "
                  "{\"action\":\"finish\",\"checkpoint_index\":0,\"reason\":\"...\"}. "
                  "Every click, type, and extract MUST include the numeric index from controls. "
                  "Type MUST include a declared parameter name, never a literal value. "
                  "Extract MUST include a declared output name. When all outputs are extracted and the goal is met, "
                  "finish MUST include checkpoint_index for a visible heading or stable control proving success. "
                  "Include a brief reason in every decision without sensitive values. "
                  "Never use a risky control. If stuck reply {\"action\":\"stuck\",\"reason\":\"...\"}. "
                  "Use only listed indices and declared parameters/outputs. "
                  "The recent_steps list records actions already done. Do not repeat a successful action; "
                  "advance to the next needed control.")
        if recent_steps:
            last = recent_steps[-1]
            if last.action == "type":
                system += (" The most recent action already typed the declared parameter successfully. "
                           "Your next action MUST advance the workflow using an appropriate visible control. "
                           "Do not choose type again.")
            elif last.action == "click":
                system += (" The most recent click already advanced the workflow. "
                           "If the requested output is visible, your next action MUST be extract. "
                           "Do not choose type again.")
            elif last.action == "extract" and set(completed) == {p.name for p in outputs}:
                system += (" All outputs are extracted. Your next action MUST be finish "
                           "with checkpoint_index for a visible success marker.")
        user = {"goal": goal, "observation": observation,
                "inputs": [p.model_dump(mode="json") for p in inputs],
                "outputs": [p.model_dump(mode="json") for p in outputs],
                "extracted": completed,
                "recent_steps": [{"action": step.action, "target": step.target.description,
                                  "parameter": step.value.parameter if step.value else None,
                                  "output": step.output} for step in recent_steps[-5:]]}
        payload = {"model": self.model, "temperature": 0,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": json.dumps(user)}]}
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(self.url, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("model decision must be a JSON object")
            if result.get("action") != "extract":
                return result
            if not result.get("output") and len(outputs) == 1:
                result["output"] = outputs[0].name
            allowed = {item.name: item for item in outputs}
            selected = allowed.get(result.get("output"))
            try:
                control = observation["controls"][int(result["index"])]
            except (IndexError, ValueError, TypeError, KeyError):
                control = {}
            label = " ".join(str(control.get(key) or "") for key in
                             ("row_label", "label", "text", "name")).lower()
            hints = set(re.findall(r"[a-z]{4,}",
                                   f"{selected.name} {selected.description}".lower())) if selected else set()
            hints -= {"displayed", "current", "value", "field", "account", "member", "customer"}
            if selected and control and (not hints or any(hint in label for hint in hints)):
                return result
            if attempt == 1:
                raise ValueError("model extract decision did not match a declared output and observed label: "
                                 f"output={result.get('output')!r}, index={result.get('index')!r}, "
                                 f"control_label={label!r}")
            payload["messages"].append({"role": "user", "content":
                "The proposed extract is invalid. Use exactly one declared output name and "
                "choose its matching labeled control in the current observation. "
                f"Allowed outputs: {json.dumps([item.model_dump(mode='json') for item in outputs])}. "
                "Do not extract an input identifier or its echoed value. Reply with a corrected JSON decision."})
        raise AssertionError("unreachable decision loop")


def default_conditions() -> tuple[list[Condition], list[Condition], list[Condition]]:
    def condition(code: str, text: str) -> Condition:
        return Condition(code=code, description=text,
                         target=Target(description=text, candidates=[Locator(strategy="text", value=text, tag="p")]))
    return ([condition("member_not_found", "Member not found"),
             condition("validation_error", "Invalid member ID")],
            [condition("session_expired", "Session expired"),
             condition("verification_required", "Verification required")],
            [condition("permission_denied", "Permission denied"),
             condition("application_error", "Application error")])


def compile_discovery(req: DiscoveryRequest, steps: list[Step], checkpoint: Target,
                      tenant_id: int, fingerprint: str, ui_version: str | None) -> tuple[BaseCapability, TenantBinding]:
    """Separate logical behavior from the observed tenant's selectors."""
    targets: dict[str, Target] = {}
    logical_steps: list[LogicalStep] = []
    for step in steps:
        if step.action == "type" and step.value and step.value.parameter == "member_id":
            name = "member_input"
        elif step.action == "click" and "search" in step.target.description.lower():
            name = "search_button"
        elif step.action == "extract" and step.output == "balance":
            name = "balance_cell"
        else:
            name = f"control_{step.id}"
        if name in targets and targets[name] != step.target:
            raise ValueError("logical control name has conflicting selectors")
        targets[name] = step.target
        logical_steps.append(LogicalStep(id=step.id, action=step.action, control=name,
                                         value=step.value, output=step.output, risk=step.risk))
    targets["success_marker"] = checkpoint
    business, recoverable, hard = default_conditions()
    def convert(items: list[Condition]) -> list[LogicalCondition]:
        for item in items:
            targets[item.code] = item.target
        return [LogicalCondition(code=item.code, control=item.code,
                                 description=item.description) for item in items]
    base = BaseCapability(name=req.capability_name, description=redacted(req.goal, req.values),
                          vendor_product="local-bank-demo", vendor_version="1",
                          inputs=req.inputs, outputs=req.outputs, steps=logical_steps,
                          checkpoint="success_marker", business_outcomes=convert(business),
                          recoverable=convert(recoverable), hard_failures=convert(hard))
    binding = TenantBinding(base_id=base.id, tenant_id=tenant_id,
                            entry_url=req.target_url, allowed_origin=origin(req.target_url),
                            ui_fingerprint=fingerprint, ui_version=ui_version, targets=targets,
                            approval_state="draft")
    return base, binding


@dataclass
class RunSession:
    id: str
    mode: str
    browser: BrowserSurface
    policy: Policy
    values: dict[str, Any]
    actor_id: int
    tenant_id: int
    browser_session_id: str = field(default_factory=lambda: str(uuid4()))
    request: DiscoveryRequest | None = None
    capability: Capability | None = None
    steps: list[Step] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    intervention: Intervention | None = None
    owner: str = "automation"
    next_step: int = 0
    retries: int = 0
    result: RunResult | None = None
    assisted_fallback: bool = False
    fallback_used: bool = False
    fallback_pending: bool = False
    binding_version: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def log(self, kind: str, **detail: Any) -> None:
        self.events.append(Event(kind=kind, detail=redacted(detail, self.values)))

    async def observation(self) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return redacted(await self.browser.observe(), self.values)
            except PlaywrightError:
                if attempt == 2:
                    break
                await asyncio.sleep(0.3)
        return {"url": "[navigation in progress]", "controls": [],
                "observation_error": "browser navigation prevented a stable snapshot"}

    async def handoff(self, reason: str, step_id: str | None = None,
                      code: str = "human_required") -> RunResult:
        obs = await self.observation()
        self.intervention = Intervention(reason=reason, step_id=step_id, observation=obs,
                                         browser_session_id=self.browser_session_id)
        self.owner = "human"
        self.log("intervention_requested", reason=reason, code=code, step_id=step_id,
                 owner=self.owner, browser_session_id=self.browser_session_id, observation=obs)
        return RunResult(status="intervention_required", code=code, step_id=step_id,
                         observed=reason, intervention_id=self.intervention.id,
                         capability_id=self.capability.id if self.capability else None)

    def controls(self, obs: dict[str, Any]) -> list[dict[str, Any]]:
        return obs["controls"]

    async def check_browser(self) -> None:
        self.policy.check_url(await self.browser.url())


async def discover(session: RunSession, model: ModelClient, artifact_dir: Path) -> RunResult:
    req = session.request
    assert req is not None
    async with session.lock:
        if session.owner != "automation":
            return RunResult(status="intervention_required", intervention_id=session.intervention.id if session.intervention else None)
        for _ in range(req.max_steps - len(session.steps)):
            try:
                await session.check_browser()
                obs = await session.observation()
                decision = await model.decide(req.goal, obs, req.inputs, req.outputs,
                                              list(session.outputs), session.steps)
                action = decision.get("action")
                if action == "type" and not decision.get("parameter") and len(req.inputs) == 1:
                    decision["parameter"] = req.inputs[0].name
                if action == "extract" and not decision.get("output") and len(req.outputs) == 1:
                    decision["output"] = req.outputs[0].name
                session.log("model_decision", action=action, index=decision.get("index"),
                            parameter=decision.get("parameter"), output=decision.get("output"),
                            reason=decision.get("reason"))
                if action == "stuck":
                    return await session.handoff(str(decision.get("reason", "model is stuck")))
                if action == "finish":
                    if set(session.outputs) != {p.name for p in req.outputs}:
                        return await session.handoff("model declared success before extracting all outputs")
                    index = int(decision["checkpoint_index"])
                    checkpoint = target_from_control(obs["controls"][index])
                    if not await session.browser.exists(checkpoint):
                        return await session.handoff("proposed success checkpoint is absent")
                    base, binding = compile_discovery(req, session.steps, checkpoint,
                                                       session.tenant_id,
                                                       obs["title"], obs.get("ui_version"))
                    store = ArtifactStore(artifact_dir)
                    store.save_base(base)
                    store.save_binding(binding)
                    capability = binding.resolve(base)
                    session.capability = capability
                    session.log("capability_saved", capability_id=capability.id)
                    return RunResult(status="success", capability_id=capability.id, outputs=session.outputs)
                if action not in {"click", "type", "extract"}:
                    return await session.handoff("model returned unknown action")
                index = int(decision["index"])
                control = obs["controls"][index]
                session.policy.check_action(action, control)
                target = target_from_control(control)
                step = Step(id=f"step_{len(session.steps)+1}", action=action, target=target,
                            value=InputRef(parameter=decision["parameter"]) if action == "type" else None,
                            output=decision.get("output") if action == "extract" else None)
                if step.value and step.value.parameter not in req.values:
                    raise ValueError("model selected undeclared parameter")
                if step.output and step.output not in {p.name for p in req.outputs}:
                    raise ValueError("model selected undeclared output")
                if action == "click":
                    chosen = await session.browser.click(target)
                elif action == "type":
                    chosen = await session.browser.type(target, str(req.values[step.value.parameter]))
                else:
                    value, chosen = await session.browser.text(target)
                    session.outputs[step.output] = value
                session.steps.append(step)
                session.log("step_completed", step_id=step.id, action=action,
                            locator=chosen.model_dump(), output_name=step.output)
                await session.check_browser()
            except (ValueError, KeyError, IndexError, TypeError, PermissionError, DriverError,
                    httpx.HTTPError, json.JSONDecodeError, PlaywrightError, OSError) as error:
                return await session.handoff(f"discovery stopped: {type(error).__name__}: {error}")
        return await session.handoff("maximum discovery steps reached")


async def classify(browser: BrowserSurface, conditions: list[Condition]) -> Condition | None:
    for condition in conditions:
        if await browser.exists(condition.target):
            return condition
    return None


async def replay(session: RunSession, model: ModelClient | None = None) -> RunResult:
    cap = session.capability
    assert cap is not None
    async with session.lock:
        if session.owner != "automation":
            return RunResult(status="intervention_required", intervention_id=session.intervention.id if session.intervention else None)
        while session.next_step < len(cap.steps):
            step = cap.steps[session.next_step]
            try:
                await session.check_browser()
                condition = await classify(session.browser, cap.business_outcomes)
                if condition:
                    session.log("business_outcome", code=condition.code, step_id=step.id)
                    return RunResult(status="business_outcome", capability_id=cap.id,
                                     code=condition.code, step_id=step.id)
                condition = await classify(session.browser, cap.hard_failures)
                if condition:
                    session.log("hard_failure", code=condition.code, step_id=step.id,
                                observation=await session.observation())
                    return RunResult(status="failure", capability_id=cap.id, code=condition.code,
                                     step_id=step.id, expected="No application failure",
                                     observed=condition.description)
                condition = await classify(session.browser, cap.recoverable)
                if condition:
                    if condition.code == "verification_required":
                        return await session.handoff(condition.description, step.id, condition.code)
                    if condition.code == "session_expired" and session.retries < 1:
                        session.retries += 1
                        session.log("recovery", code=condition.code, attempt=session.retries)
                        await session.browser.navigate(cap.entry_url)
                        session.next_step = 0
                        session.outputs.clear()
                        continue
                    return await session.handoff(condition.code, step.id, "recovery_exhausted")
                if step.risk == "risky":
                    session.log("policy_blocked", step_id=step.id, reason="recorded risky step")
                    return await session.handoff("recorded risky step requires human", step.id, "policy_blocked")
                target = step.target
                try:
                    control = await session.browser.control(target)
                except DriverError as missing:
                    if not session.assisted_fallback or session.fallback_used or model is None:
                        raise
                    session.fallback_used = True
                    obs = await session.observation()
                    try:
                        index = await model.propose_locator(obs, target.description, step.action)
                        if index < 0:
                            raise ValueError("model declined correction")
                        proposed = target_from_control(obs["controls"][index])
                        control = await session.browser.control(proposed)
                        if step.action == "type" and control.get("tag") not in {"input", "textarea"}:
                            raise ValueError("proposed control cannot accept typed input")
                        if step.action == "click" and control.get("tag") not in {"button", "a", "input"}:
                            raise ValueError("proposed control is not an actionable click target")
                        session.policy.check_action(step.action, control)
                        if step.action == "click":
                            destination = await session.browser.destination(proposed)
                            if destination:
                                session.policy.check_url(destination)
                        target = proposed
                        session.fallback_pending = True
                        session.log("fallback_proposed", step_id=step.id,
                                    proposed_target=proposed.model_dump(mode="json"), outcome="candidate")
                    except (ValueError, IndexError, KeyError, DriverError, PermissionError,
                            httpx.HTTPError) as error:
                        session.log("fallback_rejected", step_id=step.id,
                                    reason=str(error), attempted_target=step.target.model_dump(mode="json"))
                        raise missing from error
                try:
                    session.policy.check_action(step.action, control)
                except (ValueError, PermissionError) as error:
                    session.log("policy_blocked", step_id=step.id, reason=str(error))
                    return await session.handoff(str(error), step.id, "policy_blocked")
                if step.action == "click":
                    destination = await session.browser.destination(target)
                    if destination:
                        try:
                            session.policy.check_url(destination)
                        except ValueError as error:
                            session.log("policy_blocked", step_id=step.id, reason=str(error))
                            return await session.handoff(str(error), step.id, "policy_blocked")
                    chosen = await session.browser.click(target)
                elif step.action == "type":
                    chosen = await session.browser.type(target, str(session.values[step.value.parameter]))
                else:
                    value, chosen = await session.browser.text(target)
                    session.outputs[step.output] = value
                session.next_step += 1
                session.log("step_completed", step_id=step.id, action=step.action,
                            locator=chosen.model_dump(), output_name=step.output)
                await session.check_browser()
            except (ValueError, PermissionError, DriverError, httpx.HTTPError, KeyError, PlaywrightError) as error:
                code = "target_unavailable" if isinstance(error, DriverError) else "wait_limit_exceeded" if isinstance(error, PlaywrightError) else "replay_stopped"
                if session.fallback_pending:
                    session.log("fallback_rejected", step_id=step.id,
                                reason="candidate failed before checkpoint")
                    session.fallback_pending = False
                session.log("failure_observation", code=code, step_id=step.id,
                            attempted_target=step.target.model_dump(mode="json"),
                            observed=str(error), observation=await session.observation())
                return await session.handoff(f"replay stopped: {type(error).__name__}: {error}", step.id, code)
        condition = await classify(session.browser, cap.business_outcomes)
        if condition:
            session.log("business_outcome", code=condition.code)
            return RunResult(status="business_outcome", capability_id=cap.id, code=condition.code)
        if not await session.browser.exists(cap.checkpoint):
            return await session.handoff("success checkpoint missing", "checkpoint")
        session.log("checkpoint_verified", target=cap.checkpoint.description)
        if session.fallback_pending:
            session.log("fallback_accepted", reason="final checkpoint verified; artifact unchanged")
        session.log("replay_success", capability_id=cap.id, output_names=list(session.outputs))
        return RunResult(status="success", capability_id=cap.id, outputs=session.outputs)
