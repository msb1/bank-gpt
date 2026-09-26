from __future__ import annotations

from uuid import uuid4
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bank_gpt import api
from bank_gpt.artifacts import ArtifactStore
from bank_gpt.tenant import GATEWAY, issue_token


def test_catalog_qualifies_duplicate_names_and_filters_tenant(tmp_path, monkeypatch):
    source = api.STORE
    base = source.base("d847b7f4-6460-433f-b53c-4f4640c2d352")
    binding = source.binding(base.id, 1)
    store = ArtifactStore(tmp_path)
    for copy_id in (base.id, str(uuid4())):
        store.save_base(base.model_copy(update={"id": copy_id}))
        store.save_binding(binding.model_copy(update={"base_id": copy_id, "version": 1}))
    monkeypatch.setattr(api, "STORE", store)
    actor = GATEWAY.authenticate("north_reader", "north-demo-pass")
    assert actor
    headers = {"Authorization": "Bearer " + issue_token(actor, audience="bank-gpt-api")}
    with TestClient(api.app) as client:
        response = client.get("/capabilities/catalog?tenant_id=1", headers=headers)
        assert response.status_code == 200
        entries = response.json()["capabilities"]
        assert len(entries) == 2
        assert len({item["invoke_name"] for item in entries}) == 2
        assert all(item["name"] == "read_savings_balance" for item in entries)
        assert client.get("/capabilities/catalog?tenant_id=2", headers=headers).status_code == 403
        assert client.post("/capabilities/invoke", headers=headers, json={
            "tenant_id": 1, "name": entries[0]["invoke_name"], "values": {"member_id": "10001"}
        }).status_code == 401


def test_binding_versions_keep_draft_approval_and_revocation(tmp_path):
    source = api.STORE
    base = source.base("d847b7f4-6460-433f-b53c-4f4640c2d352")
    current = source.binding(base.id, 1)
    store = ArtifactStore(tmp_path)
    store.save_base(base)
    draft = current.model_copy(update={"version": 1, "approval_state": "draft",
                                       "reviewed_by": None, "reviewed_at": None,
                                       "stability": None})
    store.save_binding(draft)
    assert store.catalog(1) == []
    approved = draft.model_copy(update={"version": 2, "approval_state": "approved",
                                         "reviewed_by": 5,
                                         "reviewed_at": datetime.now(timezone.utc)})
    store.review_binding(approved)
    assert len(store.catalog(1)) == 1
    revoked = approved.model_copy(update={"version": 3, "approval_state": "revoked"})
    store.review_binding(revoked)
    assert store.catalog(1) == []
    assert store.binding(base.id, 1).approval_state == "revoked"
    assert '"approval_state": "draft"' in store.binding_version_path(base.id, 1, 1).read_text()
    assert '"approval_state": "approved"' in store.binding_version_path(base.id, 1, 2).read_text()
    assert '"approval_state": "revoked"' in store.binding_version_path(base.id, 1, 3).read_text()


def test_evaluation_grant_only_for_draft_binding(tmp_path, monkeypatch):
    source = api.STORE
    base = source.base("d847b7f4-6460-433f-b53c-4f4640c2d352")
    binding = source.binding(base.id, 1)
    store = ArtifactStore(tmp_path)
    store.save_base(base)
    store.save_binding(binding.model_copy(update={"version": 1, "approval_state": "draft",
                                          "stability": None}))
    monkeypatch.setattr(api, "STORE", store)
    actor = GATEWAY.authenticate("north_operator", "north-operator-pass")
    assert actor
    headers = {"Authorization": "Bearer " + issue_token(actor, audience="bank-gpt-api")}
    path = f"/capabilities/{base.id}/grants"
    with TestClient(api.app) as client:
        assert client.post(path, headers=headers, json={
            "tenant_id": 1, "operation": "evaluate"}).status_code == 200
        assert client.post(path, headers=headers, json={
            "tenant_id": 1, "operation": "replay"}).status_code == 403
        store.review_binding(store.binding(base.id, 1).model_copy(update={
            "version": 2, "approval_state": "approved"}))
        assert client.post(path, headers=headers, json={
            "tenant_id": 1, "operation": "evaluate"}).status_code == 403
