# Bank GPT: purpose and operation

## What the system does

Bank GPT operates a back-office UI when a calling agent has a task but the application has no usable API. The caller chooses the task. In **discovery**, an LLM observes the live UI and completes it, producing a typed capability artifact. Normal **replay** follows that saved artifact without model decisions; an operator can explicitly enable one assisted locator correction. If automation cannot continue safely, an authorized operator can act on the same paused browser page and hand it back.

The concrete demo looks up a fictional member's savings balance. It has three fictional institutions and 25 fictional customers. An authenticated caller selects a tenant and provides `member_id: string`; a successful capability returns `balance: string`. The discovery value `10001` becomes a parameter reference in the artifact, not a stored literal. A shared base capability has separate tenant UI bindings. See [tenant security](tenant-security.md).

## Inputs and outputs

| Stage | Input | Output |
| --- | --- | --- |
| Discovery | Bearer token; tenant ID; goal; allowed target URL; capability name; typed input/output definitions; example input values. | Run ID and structured status. Success includes a saved capability ID and declared outputs. A blocked run includes an intervention ID. |
| Replay | Identity bearer token; two-minute capability grant; base ID; tenant ID; values for declared parameters. | Run ID and `success` with outputs, `business_outcome`, `failure`, or `intervention_required`. |
| Run inspection | Original caller's bearer token and run ID. | Owner, next step, pending intervention, redacted events, and the result with declared output values if the run completed. |
| Operator handoff | Operator bearer token for the run's tenant, run ID, manual actions. | Updated live UI state and a continued run result after resume. |

## Run sequence

1. The API verifies the bearer token, checks the actor's current tenant role in SQLite, validates the URL allowlist, and opens an isolated remote browser with a signed actor-and-tenant session cookie.
2. Discovery observes visible controls and sends a redacted observation plus goal and parameter schemas to the model. It accepts bounded click, type, extract, finish, and stuck decisions. It checks policy before acting.
3. On success, it verifies the checkpoint and writes a schema 2.0 base at `artifacts/{capability_id}.json` and a draft binding at `artifacts/{capability_id}.tenant-{tenant_id}.json`. A tenant operator evaluates the draft at least three times, submits the run IDs for a stability report, and approves a new binding version when the success rate meets 0.9.
4. The caller requests a short-lived grant for the base and tenant. Replay checks the identity and grant together, current membership, and binding approval, then opens a fresh browser, runs the saved steps, checks conditions and checkpoint, and returns declared outputs. Normal replay does not ask the model to choose actions.
5. A member ID from another tenant appears as `member_not_found` in the demo UI. A caller trying to start a run for a tenant where they lack membership receives HTTP 403 before Playwright opens.

## Human handoff

1. **Pause.** Discovery can stop on a stuck or malformed decision, step limit, timeout, missing checkpoint, or policy issue. Replay can stop on a missing target or checkpoint, repeated recoverable condition, verification requirement, or risky step. Bank GPT records the reason, current step, owner, non-secret browser-session ID, and a redacted observation. It changes ownership to `human` and returns `intervention_required` with a run ID and intervention ID.
2. **Inspect.** Obtain a bearer token for a user with the `operator` role in that run's tenant. Open `/runs/{run_id}/operator`, enter the token, and refresh. The page calls `GET /runs/{run_id}/operator/state` and lists the intervention and currently observed controls. The original caller can inspect redacted events through `GET /runs/{run_id}`.
3. **Act.** Click or Type on the operator page. `POST /runs/{run_id}/operator/action` uses the same browser page held by the paused run. The API verifies the operator's tenant role and current human ownership, checks action and URL policy, and logs the operator ID and action. During discovery, typing must use a declared parameter and its current value so the capability remains reusable.
4. **Resume.** The page calls `POST /runs/{run_id}/resume`. The API marks the intervention resolved, logs the operator ID, returns ownership to automation, and continues the same run. Leave **I performed the paused recorded step** unchecked to retry it. Check it only after clicking or typing the recorded step's control; the API rejects an unsupported skip with HTTP 422. The verification control is a prerequisite for extraction, so leave it unchecked in that case. A retry while verification is still pending creates another intervention.
5. **Close.** Resume returns another structured result, which may request further intervention. When done, an operator for the run's tenant calls `DELETE /runs/{run_id}` to release its browser context.

The operator page exposes observed controls rather than a visual browser stream. There is no intervention notification queue. Run state lives in the FastAPI process. An API restart loses the paused handoff; the old operator URL returns HTTP 404 and the caller must start a new run. The remote browser connection is not reattached. `evidence/handoff/restart-paused.json` and `restart-check.json` record a live before-and-after restart check.

For the fictional verification scenario, run `./.venv/bin/python scripts/run_handoff.py`. It creates a paused replay, prints the scenario/run ID and operator URL, and saves scrubbed pause state. Log in as `north_operator`, enter the bearer token on that page, click **Verify lookup**, then resume with the checkbox clear. `./.venv/bin/python scripts/run_handoff.py --auto-operator` exercises those same UI controls, reader and other-tenant operator denials, a repeated handoff, and explicit completion of a separate paused Search step; it saves traces under `evidence/handoff/`. After creating a paused run, restart the API and run `./.venv/bin/python scripts/run_handoff.py --check-restart RUN_ID` to assert HTTP 404.

## Deployment

### Remote browser

Copy `docker-compose.yaml` to the remote PC and run `docker compose up -d`. It serves Playwright on port 8080; the client and server are pinned to 1.63.0. Replace `192.168.x.xx` in the example WebSocket URL with the remote PC's reachable address. Keep the unauthenticated server on a trusted LAN or VPN with port 8080 restricted by the remote firewall.

The remote browser must reach the API machine's LAN origin. Start FastAPI with `--host 0.0.0.0` and use that LAN origin in `BANKGPT_ALLOWED_ORIGINS` and `target_url`. Remote `127.0.0.1` does not reach the API machine. The API process calls the model endpoint; the remote browser does not.

### Local service

1. Run `uv sync`. Chromium is in the remote Docker image; no local `playwright install` is needed.
2. Configure `.env` (ignored by Git). The existing model variables are `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `MAIN_MODEL`. Set the variables below; the signing key must be random and at least 32 characters.
3. Start `uv run uvicorn bank_gpt.api:app --host 0.0.0.0 --port 8000`.
4. Check `/health`. A direct request to `/demo` without a run-bound browser cookie returns 403.

| Variable | Purpose |
| --- | --- |
| `BANKGPT_PLAYWRIGHT_WS_URL` | Remote WebSocket; `ws://192.168.x.xx:8080/`. |
| `BANKGPT_ALLOWED_ORIGINS` | Allowed browser origin. With published binding substitution enabled, configure exactly one origin. |
| `BANKGPT_ARTIFACT_PUBLIC_ORIGIN` | Publication placeholder for the API origin in saved binding JSON; `http://192.168.z.zz:8000` in this repository. The current artifact store expects one allowed origin when this is set. |
| `BANKGPT_PUBLIC_PLAYWRIGHT_HOST` | Publication placeholder for the browser host in saved evidence; `192.168.x.xx`. |
| `BANKGPT_ALLOWED_ROUTES` | Comma-separated exact browser paths; include `/demo/pine` and `/demo/verify`. |
| `BANKGPT_ALLOWED_ACTIONS` | Automation actions; default `click,type,extract`. |
| `BANKGPT_SIGNING_KEY` | Secret for signed API tokens and browser cookies. |
| `BANKGPT_DEMO_DB` | Optional SQLite fixture path; default `data/mock_bank.sqlite3`. |
| `BANKGPT_ARTIFACT_DIR` | Optional artifact directory; default `artifacts/`. |
| `BANKGPT_API_URL` | FastMCP process gateway URL; default `http://127.0.0.1:8000`. |
| `BANKGPT_IDENTITY_TOKEN` | Short-lived identity token supplied by the trusted FastMCP stdio host; never store in source or evidence. |

The API loads settings from the repository `.env` when its process starts. Restart the API after changing them. The FastMCP stdio process reads `BANKGPT_API_URL` and `BANKGPT_IDENTITY_TOKEN` from the environment supplied by its trusted host. The SQLite fixture persists across API restarts; active browser runs and demo search state do not.

## Demo and failure investigation

Use the discovery and replay commands in [README.md](../README.md#discovery-approval-and-replay). A valid tenant 1 member such as `10001` should return a balance. Tenant 2 member `20001` looked up under tenant 1 should produce `member_not_found`. A tenant 1 user requesting tenant 2 directly should receive HTTP 403.

Inspect `GET /runs/{run_id}` for action, step, locator, condition, and scrubbed observation. `member_not_found` is a business result; `permission_denied` shown by the app is a hard result. `session_expired` restarts once; a repeated expiry pauses. Missing or ambiguous targets pause. If browser startup returns HTTP 502, check the remote compose logs and LAN connectivity. Store reviewed artifact and redacted event examples in `/evidence/`; keep tokens and response files with balances out of the public repository.

### Live robustness matrix

With the API and remote Playwright service running, use the reviewed fictional
capability artifact and run:

```sh
.venv/bin/python scripts/run_robustness.py --capability d847b7f4-6460-433f-b53c-4f4640c2d352
```

The runner logs in as the tenant 1 demo operator, attaches each fault to one
replay run, checks the exact result status and code, then writes scrubbed files
under `evidence/robustness/`. It fails on a mismatch. Scenario injection is
available only to operators in the requested tenant. The controller is
process-local, expires faults after five minutes or use, and removes them when
the run closes. Validation rejection is a business outcome; application error
and permission denial are hard failures. The slow cases use 2 and 8 second
delays around the browser's 5 second action wait limit.

## Agent, review, fallback, and FastMCP demonstrations

Keep the API and remote Playwright server running. The fallback demonstration also needs the configured `qwen2.5-7b-instruct-mlx` model. Run these commands from the repository root:

```sh
uv run python scripts/run_agent_mcp.py
uv run --extra test python scripts/run_codegen_demo.py
uv run python scripts/run_fallback.py
```

The FastMCP client launches the stdio server as a subprocess, discovers its three tools, selects an approved qualified name, invokes it, and reads run status. The FastMCP server obtains a fresh HTTP replay grant for the invocation. It stores token-free evidence in `evidence/agent-mcp/`. The code generator writes its input metadata and a pytest snippet to `evidence/codegen/`; the demo supplies a fictional member ID and short-lived identity token through process environment, runs the snippet, and saves its verification result. The fallback runner attaches controlled locator drift, missing-target, and risky-control scenarios to separate runs and saves Qwen proposal outcomes in `evidence/fallback/`.

`scripts/run_stability_review.py` was run once for the canonical base. It created a new draft revision for each tenant, evaluated three fictional records per tenant through remote Playwright, submitted the run IDs to the API's stability endpoint, and approved both bindings from the resulting 100% reports. Its saved report is `evidence/stability/review.json`; repeated runs will create further binding versions. The approval endpoint requires a report covering at least three matching runs and a success rate of at least 0.9. Use the catalog to confirm the current approved binding version before generating a version-bound check.

FastMCP uses stdio because the client and MCP process run on the same machine. The MCP process sends requests to `BANKGPT_API_URL` (default `http://127.0.0.1:8000`). The trusted host sets `BANKGPT_IDENTITY_TOKEN` in the child environment. For every invocation, the MCP process obtains a fresh scoped grant through the API and forwards identity and grant in HTTP headers. Neither token appears in tool arguments or saved evidence. A tool call is not an alternate authorization path: the API still checks membership, grant scope, approval, typed values, and run ownership. Restrict the stdio process and its environment to the intended host user.
