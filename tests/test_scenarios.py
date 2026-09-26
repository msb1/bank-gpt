"""Scenario authorization and isolation at the API and controller boundaries."""
import os
from fastapi.testclient import TestClient

from bank_gpt.api import app
from bank_gpt.artifacts import ArtifactStore
from bank_gpt.models import BaseCapability, LogicalStep, Locator, Parameter, Target, TenantBinding
from bank_gpt.scenarios import ScenarioController, ScenarioSpec

DEMO_ORIGIN = os.environ["BANKGPT_ALLOWED_ORIGINS"].split(",")[0]


def test_controller_binds_run_actor_tenant_and_consumes_once():
    controller = ScenarioController()
    spec = ScenarioSpec(fault="validation_error", step_id="step_2")
    controller.attach("run-1", 1, 1, spec)
    assert controller.get("run-1", 2, 1, "step_2") is None
    assert controller.get("run-1", 1, 2, "step_2") is None
    assert controller.get("run-2", 1, 1, "step_2") is None
    assert controller.get("run-1", 1, 1, "step_1") is None
    assert controller.consume("run-1", 1, 1, "step_2") == spec
    assert controller.consume("run-1", 1, 1, "step_2") is None


def test_reader_cannot_attach_scenario_before_browser_start(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    base = BaseCapability(name="test_lookup", description="Test", vendor_product="local-bank-demo",
                          vendor_version="1", inputs=[Parameter(name="member_id", type="string", description="ID")],
                          outputs=[Parameter(name="balance", type="string", description="Balance")],
                          steps=[LogicalStep(id="step_1", action="type", control="member_input",
                                             value={"parameter": "member_id"})], checkpoint="success_marker")
    target = Target(description="Member ID", candidates=[Locator(strategy="name", value="member_id")])
    store.save_base(base)
    store.save_binding(TenantBinding(base_id=base.id, tenant_id=1,
                                     entry_url=f"{DEMO_ORIGIN}/demo",
                                     allowed_origin=DEMO_ORIGIN,
                                     ui_fingerprint="Member Service Console",
                                     ui_version="north-v1",
                                     targets={"member_input": target, "success_marker": target},
                                     approval_state="approved"))
    monkeypatch.setattr("bank_gpt.api.STORE", store)
    with TestClient(app) as client:
        login = client.post("/auth/token", json={"username": "north_reader",
                                                 "password": "north-demo-pass"})
        token = login.json()["access_token"]
        grant = client.post(f"/capabilities/{base.id}/grants",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"tenant_id": 1}).json()["secure_permissions"]
        response = client.post(f"/capabilities/{base.id}/replay",
                               headers={"Authorization": f"Bearer {token}",
                                        "X-Secure-Permissions": grant},
                               json={"tenant_id": 1, "values": {"member_id": "10001"},
                                     "scenario": {"fault": "validation_error", "step_id": "step_2"}})
        assert response.status_code == 403
