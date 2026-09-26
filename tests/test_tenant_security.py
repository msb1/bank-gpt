"""Security boundaries for the fictional institution fixture."""
import os
from fastapi.testclient import TestClient

from bank_gpt.api import POLICY, RUNS, app
from bank_gpt.core import RunSession
from bank_gpt.tenant import GATEWAY, issue_token

DEMO_ORIGIN = os.environ["BANKGPT_ALLOWED_ORIGINS"].split(",")[0]


def token(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/token", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def test_fixture_has_25_customers_and_foreign_keys():
    with GATEWAY.connect() as db:
        assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 25
        assert db.execute("SELECT count(*) FROM tenants").fetchone()[0] == 3
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_demo_requires_bound_browser_identity_and_hides_other_tenant_records():
    with TestClient(app) as client:
        assert client.get("/demo").status_code == 403
        actor = GATEWAY.authenticate("north_reader", "north-demo-pass")
        assert actor is not None
        client.cookies.set("bankgpt_browser", issue_token(actor, audience="bank-gpt-browser", tenant_id=1, run_id="test-run"))
        assert client.get("/demo").status_code == 200
        own = client.post("/demo/search", data={"member_id": "10001"})
        assert "Savings balance" in own.text
        other = client.post("/demo/search", data={"member_id": "20001"})
        assert "Member not found" in other.text
        assert "Example Member 2-01" not in other.text
        client.cookies.set("bankgpt_browser", issue_token(actor, audience="bank-gpt-browser", tenant_id=2, run_id="test-run"))
        assert client.get("/demo").status_code == 403


def test_api_and_browser_tokens_have_distinct_audiences():
    actor = GATEWAY.authenticate("north_reader", "north-demo-pass")
    assert actor is not None
    with TestClient(app) as client:
        api_token = issue_token(actor, audience="bank-gpt-api")
        client.cookies.set("bankgpt_browser", api_token)
        assert client.get("/demo").status_code == 403
        browser_token = issue_token(actor, audience="bank-gpt-browser", tenant_id=1)
        response = client.get("/runs/unknown",
                              headers={"Authorization": f"Bearer {browser_token}"})
        assert response.status_code == 401


def test_cross_tenant_discovery_rejected_before_browser_start():
    with TestClient(app) as client:
        access = token(client, "north_reader", "north-demo-pass")
        response = client.post("/discover", headers={"Authorization": f"Bearer {access}"}, json={
            "goal": "Read the member savings balance", "tenant_id": 2,
            "target_url": f"{DEMO_ORIGIN}/demo", "capability_name": "read_balance",
            "inputs": [{"name": "member_id", "type": "string", "description": "Member ID"}],
            "values": {"member_id": "20001"},
            "outputs": [{"name": "balance", "type": "string", "description": "Savings balance"}],
        })
        assert response.status_code == 403


def test_operator_must_have_role_in_the_paused_runs_tenant():
    class PausedBrowser:
        async def observe(self):
            return {"url": f"{DEMO_ORIGIN}/demo", "title": "Mock",
                    "controls": []}

    starter = GATEWAY.authenticate("north_reader", "north-demo-pass")
    assert starter is not None
    session = RunSession(id="tenant-operator-check", mode="replay", browser=PausedBrowser(),
                         policy=POLICY, values={}, actor_id=starter.id, tenant_id=1,
                         owner="human")
    RUNS[session.id] = session
    try:
        with TestClient(app) as client:
            for username, password in [
                ("north_reader", "north-demo-pass"),
                ("cedar_operator", "cedar-demo-pass"),
            ]:
                access = token(client, username, password)
                response = client.get(f"/runs/{session.id}/operator/state",
                                      headers={"Authorization": f"Bearer {access}"})
                assert response.status_code == 403
            access = token(client, "north_operator", "north-operator-pass")
            response = client.get(f"/runs/{session.id}/operator/state",
                                  headers={"Authorization": f"Bearer {access}"})
            assert response.status_code == 200
    finally:
        RUNS.pop(session.id, None)
