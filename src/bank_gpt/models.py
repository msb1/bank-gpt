from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .scenarios import ScenarioSpec


class ValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class Parameter(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType
    description: str
    required: bool = True


class Locator(BaseModel):
    strategy: Literal["id", "name", "label", "text", "css", "xpath"]
    value: str
    tag: str | None = None


class Target(BaseModel):
    description: str
    candidates: list[Locator] = Field(min_length=1)


class InputRef(BaseModel):
    parameter: str


class Step(BaseModel):
    id: str
    action: Literal["click", "type", "extract"]
    target: Target
    value: InputRef | None = None
    output: str | None = None
    expected: str | None = None
    risk: Literal["safe", "risky"] = "safe"

    @model_validator(mode="after")
    def check_fields(self) -> "Step":
        if self.action == "type" and self.value is None:
            raise ValueError("type requires a parameter reference")
        if self.action == "extract" and self.output is None:
            raise ValueError("extract requires an output name")
        return self


class Condition(BaseModel):
    code: str
    target: Target
    description: str


class LogicalStep(BaseModel):
    id: str
    action: Literal["click", "type", "extract"]
    control: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: InputRef | None = None
    output: str | None = None
    risk: Literal["safe", "risky"] = "safe"


class LogicalCondition(BaseModel):
    code: str
    control: str
    description: str


class BaseCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str
    vendor_product: str
    vendor_version: str
    surface: Literal["playwright"] = "playwright"
    inputs: list[Parameter]
    outputs: list[Parameter]
    steps: list[LogicalStep] = Field(min_length=1)
    checkpoint: str
    business_outcomes: list[LogicalCondition] = Field(default_factory=list)
    recoverable: list[LogicalCondition] = Field(default_factory=list)
    hard_failures: list[LogicalCondition] = Field(default_factory=list)
    allowed_actions: set[Literal["click", "type", "extract"]] = Field(default_factory=lambda: {"click", "type", "extract"})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def contract(self) -> "BaseCapability":
        inputs = {item.name for item in self.inputs}
        outputs = {item.name for item in self.outputs}
        if len(inputs) != len(self.inputs) or len(outputs) != len(self.outputs):
            raise ValueError("duplicate input or output name")
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("duplicate step ID")
        for step in self.steps:
            if step.action not in self.allowed_actions:
                raise ValueError("step exceeds base action policy")
            if step.action == "type" and (not step.value or step.value.parameter not in inputs):
                raise ValueError("type step requires a declared input")
            if step.action == "extract" and step.output not in outputs:
                raise ValueError("extract step requires a declared output")
        return self

    @property
    def controls(self) -> set[str]:
        return ({step.control for step in self.steps} | {self.checkpoint} |
                {item.control for item in self.business_outcomes + self.recoverable + self.hard_failures})


class TenantBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2.0"] = "2.0"
    base_id: str
    tenant_id: int = Field(gt=0)
    version: int = Field(default=1, ge=1)
    entry_url: str
    allowed_origin: str
    ui_fingerprint: str
    ui_version: str | None
    targets: dict[str, Target]
    reviewed_overrides: dict[str, Target] = Field(default_factory=dict)
    approval_state: Literal["draft", "approved", "revoked"] = "draft"
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    stability: dict[str, Any] | None = None

    def resolve(self, base: BaseCapability) -> "Capability":
        if self.base_id != base.id or set(self.targets) != base.controls:
            raise ValueError("binding does not cover the base controls")
        if set(self.reviewed_overrides) - base.controls:
            raise ValueError("override refers to unknown base control")
        targets = self.targets | self.reviewed_overrides
        def conditions(items: list[LogicalCondition]) -> list[Condition]:
            return [Condition(code=item.code, description=item.description,
                              target=targets[item.control]) for item in items]
        return Capability(id=base.id, name=base.name, description=base.description,
                          tenant_id=self.tenant_id, created_by=self.reviewed_by or 0,
                          vendor_product=base.vendor_product, vendor_version=base.vendor_version,
                          allowed_origin=self.allowed_origin, entry_url=self.entry_url,
                          inputs=base.inputs, outputs=base.outputs,
                          steps=[Step(id=item.id, action=item.action, target=targets[item.control],
                                      value=item.value, output=item.output, risk=item.risk)
                                 for item in base.steps], checkpoint=targets[base.checkpoint],
                          business_outcomes=conditions(base.business_outcomes),
                          recoverable=conditions(base.recoverable),
                          hard_failures=conditions(base.hard_failures))


class Capability(BaseModel):
    """Resolved in-memory execution plan; never stored as a base artifact."""
    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str
    tenant_id: int
    created_by: int
    vendor_product: str = "local-bank-demo"
    vendor_version: str = "1"
    surface: Literal["playwright"] = "playwright"
    allowed_origin: str
    entry_url: str
    inputs: list[Parameter]
    outputs: list[Parameter]
    steps: list[Step] = Field(min_length=1)
    checkpoint: Target
    business_outcomes: list[Condition] = Field(default_factory=list)
    recoverable: list[Condition] = Field(default_factory=list)
    hard_failures: list[Condition] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def contract(self) -> "Capability":
        inputs = {item.name for item in self.inputs}
        outputs = {item.name for item in self.outputs}
        if len(inputs) != len(self.inputs) or len(outputs) != len(self.outputs):
            raise ValueError("duplicate input or output name")
        if any(step.value and step.value.parameter not in inputs for step in self.steps):
            raise ValueError("step references undeclared input")
        if any(step.output and step.output not in outputs for step in self.steps):
            raise ValueError("step references undeclared output")
        return self


class DiscoveryRequest(BaseModel):
    goal: str = Field(min_length=5)
    tenant_id: int = Field(gt=0)
    target_url: str
    capability_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    inputs: list[Parameter]
    values: dict[str, Any]
    outputs: list[Parameter]
    max_steps: int = Field(default=12, ge=1, le=30)
    timeout_seconds: int = Field(default=120, ge=10, le=600)


class ReplayRequest(BaseModel):
    tenant_id: int = Field(gt=0)
    values: dict[str, Any]
    scenario: ScenarioSpec | None = None
    evaluation: bool = False
    assisted_fallback: bool = False
    expected_binding_version: int | None = Field(default=None, ge=1)


class Event(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    status: Literal["success", "business_outcome", "failure", "intervention_required"]
    capability_id: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    code: str | None = None
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    intervention_id: str | None = None


class Intervention(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    reason: str
    browser_session_id: str
    step_id: str | None = None
    owner: Literal["automation", "human"] = "human"
    state: Literal["open", "resolved"] = "open"
    observation: dict[str, Any]
    completed_step_action: bool = False


def validate_values(specs: list[Parameter], values: dict[str, Any]) -> None:
    expected = {item.name for item in specs}
    if set(values) - expected:
        raise ValueError(f"undeclared inputs: {', '.join(sorted(set(values) - expected))}")
    for spec in specs:
        if spec.required and spec.name not in values:
            raise ValueError(f"missing input: {spec.name}")
        if spec.name not in values:
            continue
        value = values[spec.name]
        valid = ((spec.type == ValueType.STRING and isinstance(value, str)) or
                 (spec.type == ValueType.NUMBER and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                 (spec.type == ValueType.BOOLEAN and isinstance(value, bool)))
        if not valid:
            raise ValueError(f"{spec.name} must be {spec.type.value}")
