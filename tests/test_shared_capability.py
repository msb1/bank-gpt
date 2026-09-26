"""Shared contract, tenant bindings, and scoped grant boundaries."""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from bank_gpt.api import app
from bank_gpt.artifacts import ArtifactError, ArtifactStore
from bank_gpt.models import (BaseCapability, Locator, LogicalStep, Parameter,
                             Target, TenantBinding)
from bank_gpt.tenant import GATEWAY, ISSUER, issue_token, verify_token

DEMO_ORIGIN = os.environ["BANKGPT_ALLOWED_ORIGINS"].split(",")[0]


def fixture_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    base = BaseCapability(name="read_savings_balance", description="Read balance",
                          vendor_product="local-bank-demo", vendor_version="1",
                          inputs=[Parameter(name="member_id", type="string", description="Member ID")],
                          outputs=[Parameter(name="balance", type="string", description="Balance")],
                          steps=[LogicalStep(id="step_1", action="type", control="member_input",
                                             value={"parameter": "member_id"}),
                                 LogicalStep(id="step_2", action="click", control="search_button"),
                                 LogicalStep(id="step_3", action="extract", control="balance_cell",
                                             output="balance")], checkpoint="success_marker")
    store.save_base(base)
    for tenant_id, label in [(1, "Member ID"), (2, "Customer reference")]:
        targets = {name: Target(description=label, candidates=[Locator(strategy="text", value=label)])
                   for name in base.controls}
        store.save_binding(TenantBinding(base_id=base.id, tenant_id=tenant_id,
                                         entry_url=f"{DEMO_ORIGIN}/demo{'/pine' if tenant_id == 2 else ''}",
                                         allowed_origin=DEMO_ORIGIN,
                                         ui_fingerprint="Pine Valley Account Desk" if tenant_id == 2 else "Member Service Console",
                                         ui_version="pine-v2" if tenant_id == 2 else "north-v1",
                                         targets=targets, approval_state="approved"))
    return store, base


def test_binding_changes_selectors_without_changing_base_contract(tmp_path):
    store, base = fixture_artifact(tmp_path)
    stored = store.binding_path(base.id, 1).read_text()
    assert os.environ["BANKGPT_ARTIFACT_PUBLIC_ORIGIN"] in stored
    assert DEMO_ORIGIN not in stored
    first = store.binding(base.id, 1).resolve(base)
    second = store.binding(base.id, 2).resolve(base)
    assert first.entry_url == f"{DEMO_ORIGIN}/demo"
    assert first.id == second.id == base.id
    assert first.inputs == second.inputs and first.outputs == second.outputs
    assert first.steps[0].target != second.steps[0].target
    assert store.base(base.id) == base
    broken = store.binding(base.id, 2).model_copy(update={"targets": {}})
    with pytest.raises(ValueError, match="cover"):
        broken.resolve(base)
    widened = store.binding(base.id, 2).model_copy(update={"reviewed_overrides": {"new_action": first.steps[0].target}})
    with pytest.raises(ValueError, match="unknown base control"):
        widened.resolve(base)
    with pytest.raises(ValueError, match="step exceeds"):
        base.model_copy(update={"allowed_actions": {"type"}}).contract()


def test_old_schema_is_explicitly_rejected(tmp_path):
    store = ArtifactStore(tmp_path)
    old = "ba8f8bd1-7cef-42aa-9edc-d16dbfd4e5ae"
    store.base_path(old).write_text('{"schema_version":"1.1"}')
    with pytest.raises(ArtifactError, match="rediscover as 2.0"):
        store.base(old)


def test_scoped_permission_grants(tmp_path, monkeypatch):
    store, base = fixture_artifact(tmp_path)
    monkeypatch.setattr("bank_gpt.api.STORE", store)
    north = GATEWAY.authenticate("north_reader", "north-demo-pass")
    shared = GATEWAY.authenticate("shared_reader", "shared-demo-pass")
    assert north and shared
    with TestClient(app) as client:
        north_identity = issue_token(north, audience="bank-gpt-api")
        shared_identity = issue_token(shared, audience="bank-gpt-api")
        grant_response = client.post(f"/capabilities/{base.id}/grants",
                                     headers={"Authorization": f"Bearer {north_identity}"},
                                     json={"tenant_id": 1})
        assert grant_response.status_code == 200
        grant = grant_response.json()["secure_permissions"]
        _, claims = verify_token(grant, "bank-gpt-capability")
        assert claims["typ"] == "secure_permissions"
        assert {"sub", "tenant_id", "capability_id", "operation", "aud", "exp", "jti"} <= claims.keys()
        replay = f"/capabilities/{base.id}/replay"
        payload = {"tenant_id": 1, "values": {"member_id": "10001"}}
        assert client.post(replay, json=payload,
                           headers={"Authorization": f"Bearer {north_identity}"}).status_code == 401
        for identity, permission, body, expected in [
            (shared_identity, grant, payload, 403),
            (north_identity, north_identity, payload, 403),
            (north_identity, grant, {"tenant_id": 2, "values": payload["values"]}, 403),
        ]:
            response = client.post(replay, json=body,
                                   headers={"Authorization": f"Bearer {identity}",
                                            "X-Secure-Permissions": permission})
            assert response.status_code == expected
        assert client.post(f"/capabilities/{base.id}/grants",
                           headers={"Authorization": f"Bearer {north_identity}"},
                           json={"tenant_id": 2}).status_code == 403
        other_base = BaseCapability.model_validate({**base.model_dump(), "id": "00000000-0000-4000-8000-000000000001"})
        store.save_base(other_base)
        store.save_binding(store.binding(base.id, 1).model_copy(update={"base_id": other_base.id}))
        assert client.post(f"/capabilities/{other_base.id}/replay", json=payload,
                           headers={"Authorization": f"Bearer {north_identity}",
                                    "X-Secure-Permissions": grant}).status_code == 403
        expired = jwt.encode({**claims, "iat": int(time.time()) - 300,
                              "nbf": int(time.time()) - 300, "exp": int(time.time()) - 1},
                             os.environ["BANKGPT_SIGNING_KEY"], algorithm="HS256")
        assert client.post(replay, json=payload,
                           headers={"Authorization": f"Bearer {north_identity}",
                                    "X-Secure-Permissions": expired}).status_code == 403


def test_token_types_and_revoked_membership(tmp_path, monkeypatch):
    store, base = fixture_artifact(tmp_path)
    monkeypatch.setattr("bank_gpt.api.STORE", store)
    actor = GATEWAY.authenticate("north_reader", "north-demo-pass")
    assert actor
    identity = issue_token(actor, audience="bank-gpt-api")
    grant = GATEWAY.issue_permission(actor, 1, base.id)
    browser = issue_token(actor, audience="bank-gpt-browser", tenant_id=1, run_id="test")
    for candidate, audience in [(grant, "bank-gpt-api"), (identity, "bank-gpt-capability"),
                                (browser, "bank-gpt-capability")]:
        with pytest.raises(PermissionError):
            verify_token(candidate, audience)
    with GATEWAY.connect() as db:
        role = db.execute("SELECT role FROM user_tenant_roles WHERE user_id=? AND tenant_id=1",
                          (actor.id,)).fetchone()[0]
        db.execute("DELETE FROM user_tenant_roles WHERE user_id=? AND tenant_id=1", (actor.id,))
    try:
        with TestClient(app) as client:
            response = client.post(f"/capabilities/{base.id}/replay",
                                   json={"tenant_id": 1, "values": {"member_id": "10001"}},
                                   headers={"Authorization": f"Bearer {identity}",
                                            "X-Secure-Permissions": grant})
            assert response.status_code == 403
    finally:
        with GATEWAY.connect() as db:
            db.execute("INSERT INTO user_tenant_roles VALUES (?,?,?)", (actor.id, 1, role))
