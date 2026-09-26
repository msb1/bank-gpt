# HTTP API

Base URL: `http://API_HOST:8000`. OpenAPI is at `/openapi.json`; interactive documentation is at `/docs`. The local fixture issues signed bearer tokens through `POST /auth/token` with `{"username":"north_reader","password":"north-demo-pass"}`. Its response has `access_token`, `token_type`, and `expires_in`. Send `Authorization: Bearer TOKEN` on control requests. See [tenant security](tenant-security.md) for users, roles, and limits.

## Discovery

`POST /discover` accepts:

```json
{
  "goal": "Look up a member and read the current savings balance",
  "tenant_id": 1,
  "target_url": "http://API_MACHINE_LAN_IP:8000/demo",
  "capability_name": "read_savings_balance",
  "inputs": [{"name": "member_id", "type": "string", "description": "Member identifier"}],
  "values": {"member_id": "10001"},
  "outputs": [{"name": "balance", "type": "string", "description": "Displayed savings balance"}],
  "max_steps": 12,
  "timeout_seconds": 120
}
```

The caller must have `capability:run` in `tenant_id`. The response is `{"run_id":"...","result":{...}}`. A successful result contains a shared base `capability_id` and creates a draft binding for the discovery tenant; `intervention_required` leaves the same browser page open. The goal must describe a reusable task and must not include a literal member ID.

## Replay and inspection

`POST /capabilities/{capability_id}/bindings` registers a tenant binding. It requires a current operator role in that tenant and validates its base ID, control coverage, and route policy. `POST /capabilities/{capability_id}/bindings/{tenant_id}/approve` approves a draft binding for an operator in that tenant. `GET /capabilities/{capability_id}?tenant_id=1` returns the base to an authorized member with an approved binding.

The published binding JSON contains an API-origin placeholder. The artifact store resolves it from the single configured allowed origin before the API performs URL policy checks and browser replay; see [artifact format](artifact.md).

`POST /capabilities/{capability_id}/grants` accepts `{"tenant_id":1,"operation":"replay"}` with the identity bearer token. The tenant gateway checks current SQLite membership and approval, then returns a two-minute `secure_permissions` JWT. Send it in `X-Secure-Permissions` alongside `Authorization: Bearer <identity token>` to `POST /capabilities/{capability_id}/replay`. Replay accepts `{"tenant_id":1,"values":{"member_id":"10001"}}`. It verifies matching subject, tenant, base ID, operation, audience, expiry, token type, and current membership before opening Playwright. The binding supplies the route and locators; the grant only authorizes use. Normal replay follows the artifact without a model call; an operator can explicitly enable one assisted locator proposal as described below. `GET /runs/{run_id}` is limited to the actor who started the run. It returns redacted events and the run result, whose declared outputs remain unredacted for that authorized actor. `DELETE /runs/{run_id}` releases the browser context and requires an operator role in that run's tenant.

For controlled tests of the fictional demo, a tenant operator may include
`"scenario":{"fault":"session_expired","step_id":"step_2","occurrences":1}`
in a replay request. Supported faults are `validation_error`, `session_expired`,
`slow_load`, `app_error`, `missing_target`, `ambiguous_target`,
`disallowed_destination`, `risky_control`, `verification_required`, and
`locator_drift`. The slow case also requires
`delay_seconds`. Faults are bound to the generated run ID and browser identity;
readers receive HTTP 403 before a browser starts. The live matrix runner and
its expected results are in `scripts/run_robustness.py`.

| Result `status` | Meaning | Other fields |
| --- | --- | --- |
| `success` | Checkpoint verified. | `capability_id`, declared `outputs` |
| `business_outcome` | Known legitimate result, such as a member not found. | `code`, `step_id` |
| `failure` | Known hard condition, such as permission denial shown by the app. | `code`, `step_id`, `observed` |
| `intervention_required` | Automation paused for an operator. | `intervention_id`, `step_id`, `observed` |

HTTP 401 means a missing or invalid identity token, or a missing grant. HTTP 403 means tenant, role, binding approval, or grant scope is denied. HTTP 422 means invalid request data, a disallowed target, or an unsupported old artifact schema. HTTP 404 means no run or artifact exists. HTTP 409 means the binding fingerprint or expected binding version did not match. HTTP 502 means the remote browser could not be started.

## Operator handoff

`GET /runs/{run_id}/operator` serves a minimal page. An operator for the run's tenant enters their bearer token there. `GET /runs/{run_id}/operator/state` returns the intervention, non-secret browser-session ID, current step, owner, and current observed controls. `POST /runs/{run_id}/operator/action` accepts `{"action":"click","index":4}` or `{"action":"type","index":3,"value":"10001","parameter":"member_id"}`. During discovery, typing must use a declared parameter and its current value so the capability stays reusable. `POST /runs/{run_id}/resume` accepts `{"completed_step":false}` and transfers ownership back to automation. `completed_step=true` requires a matching operator action on the paused replay step; otherwise it returns HTTP 422. Reader and cross-tenant operator requests return HTTP 403.

Actions operate on the same Playwright page held by the paused run. The operator page shows observed controls, not a visual browser stream. The operator ID is recorded with action and handoff events; typed literal values are not logged.

`GET /runs/{run_id}` also reports `browser_session_id` and `current_step`. A fresh API process has no active run registry; a prior paused run returns HTTP 404 after restart.

## Agent catalog and qualified invocation

`GET /capabilities/catalog?tenant_id=1` requires an identity bearer token and current `capability:run` membership in that tenant. It returns only approved bindings. Each entry contains `id`, descriptive `name`, unique `invoke_name` in the form `name@artifact-id`, description, artifact and binding versions, and typed input and output contracts. Duplicate descriptive names remain distinct by immutable artifact ID.

`POST /capabilities/invoke` accepts `{"tenant_id":1,"name":"read_savings_balance@ARTIFACT_UUID","values":{"member_id":"10001"}}`. Send the identity token and an `X-Secure-Permissions` replay grant scoped to that entry's artifact ID and tenant. The endpoint resolves the qualified name from the authorized catalog and delegates to the same replay path as `POST /capabilities/{id}/replay`. The response is the same structured `{run_id,result}` object. An unknown qualified name returns 404, a missing grant returns 401, and invalid typed arguments return 422 before browser startup.

## Binding review and evaluation

Bindings are `draft`, `approved`, or `revoked`. Operators can submit a new draft with `POST /capabilities/{id}/bindings`, or a draft revision with `POST /capabilities/{id}/bindings/{tenant_id}/revisions`. Both preserve the base contract and enforce route policy. To evaluate a draft, request `POST /capabilities/{id}/grants` with `{"tenant_id":1,"operation":"evaluate"}` as a tenant operator, then call replay with `{"tenant_id":1,"values":{...},"evaluation":true}` and that grant. `POST /capabilities/{id}/stability` accepts `{"tenant_id":1,"run_ids":[...]}`; it checks each run's tenant, base ID, binding version, and result, records counts and success rate, and creates a new draft version with the report. Approval requires at least three evaluated runs and success rate at least 0.9. `POST /capabilities/{id}/bindings/{tenant_id}/approve` records reviewer and time in a new approved version. `POST /capabilities/{id}/bindings/{tenant_id}/revoke` creates a revoked version. Every state is retained as an immutable `vN` JSON file.

Replay optionally accepts `expected_binding_version` and returns 409 if the current binding changed. The generated pytest contract check uses this to prevent a stale version from being tested as the reviewed version. Operator replay may set `assisted_fallback=true`; this permits one model-proposed correction only after a missing locator. Fallback logs its proposal and outcome, applies policy and unique-target checks, and requires the final checkpoint. It does not modify the binding. Permission denials and risky controls do not trigger it.

## FastMCP tools

The FastMCP stdio server is `python -m bank_gpt.mcp_server`. It exposes `capability_catalog(tenant_id)`, `invoke_capability(tenant_id, name, values)`, and `run_status(run_id)`. The trusted stdio host sets `BANKGPT_IDENTITY_TOKEN` in the server process environment. For each invocation, FastMCP requests a fresh scoped replay grant from the HTTP gateway, then forwards identity and grant as separate HTTP headers. The gateway validates both again. Tokens are absent from model-visible tool schemas and saved evidence. The server does not query SQLite customer tables or expose browser actions. The demonstration client is `scripts/run_agent_mcp.py` and saves a scrubbed trace under `evidence/agent-mcp/`.
