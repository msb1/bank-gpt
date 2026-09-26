"""Process-local, run-bound faults for the fictional demo only."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator


Fault = Literal["validation_error", "session_expired", "slow_load", "app_error",
                "missing_target", "ambiguous_target", "disallowed_destination", "risky_control",
                "verification_required", "locator_drift"]


class ScenarioSpec(BaseModel):
    fault: Fault
    step_id: Literal["step_1", "step_2", "step_3"]
    occurrences: int = Field(default=1, ge=1, le=2)
    delay_seconds: float = Field(default=0, ge=0, le=8)

    @model_validator(mode="after")
    def compatible(self) -> "ScenarioSpec":
        expected = {
            "validation_error": "step_2", "session_expired": "step_2",
            "slow_load": "step_2", "app_error": "step_2",
            "missing_target": "step_1", "ambiguous_target": "step_1",
            "locator_drift": "step_1",
            "disallowed_destination": "step_2", "risky_control": "step_2",
            "verification_required": "step_3",
        }
        if self.step_id != expected[self.fault]:
            raise ValueError(f"{self.fault} must target {expected[self.fault]}")
        if self.fault != "session_expired" and self.occurrences != 1:
            raise ValueError("only session_expired supports multiple occurrences")
        if self.fault == "slow_load" and self.delay_seconds == 0:
            raise ValueError("slow_load needs delay_seconds")
        return self


@dataclass
class Scenario:
    spec: ScenarioSpec
    actor_id: int
    tenant_id: int
    expires_at: float
    remaining: int


class ScenarioController:
    def __init__(self) -> None:
        self._runs: dict[str, Scenario] = {}

    def attach(self, run_id: str, actor_id: int, tenant_id: int, spec: ScenarioSpec) -> None:
        self._runs[run_id] = Scenario(spec, actor_id, tenant_id, time.monotonic() + 300,
                                      spec.occurrences)

    def get(self, run_id: str, actor_id: int, tenant_id: int,
            step_id: str) -> ScenarioSpec | None:
        scenario = self._runs.get(run_id)
        if scenario is None:
            return None
        if scenario.expires_at <= time.monotonic():
            self._runs.pop(run_id, None)
            return None
        if scenario.actor_id != actor_id or scenario.tenant_id != tenant_id:
            return None
        if scenario.spec.step_id != step_id or scenario.remaining <= 0:
            return None
        return scenario.spec

    def consume(self, run_id: str, actor_id: int, tenant_id: int,
                step_id: str) -> ScenarioSpec | None:
        spec = self.get(run_id, actor_id, tenant_id, step_id)
        if spec:
            self._runs[run_id].remaining -= 1
        return spec

    def discard(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


SCENARIOS = ScenarioController()
