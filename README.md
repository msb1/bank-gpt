# Bank GPT Computer-Use Automation System

The documentation - including the additional markdown files below and REPORT.md - describes a system 
that can:

1. Take a goal in natural language for a target application (e.g."look up member 12345 and read their current savings balance", "open a new sub-account for this member and reach the confirmation screen", or — if you use a public proxy target — "add a specific item to the cart and reach the checkout review page").
2. Use an LLM to accomplish that goal by driving a real application surface — observing
the current state, deciding what to do, and acting. The surface may be a browser, but
treat that as one case of a more general "computer use" problem (accessibility tree,
screenshot + coordinates, OS-level automation, etc. are all fair game).
3. Record the successful run as a structured, reusable artifact — a typed, versioned
description of the flow (the steps taken, how each target element/control is identified,
and any data to extract) that is decoupled from the raw model transcript.
4. Replay that artifact deterministically — re-run the recorded flow without the LLM in
the decision loop, using stable element/control targeting, and report success/failure.
5. Escalate to a human when stuck — when the system can't safely proceed, route an
intervention request to a human operator and let them take control of the live session,
then hand control back.
6. Stay within safety guardrails throughout — respect an allowlist of what the agent is
permitted to do, and avoid leaking or persisting sensitive data (this is regulated financial
data).

The through-line is: 
The model discovers. The artifact becomes a reusable capability. Deterministic replay is
how the AI agent invokes it in production.


Bank GPT Computer-Use Automation System discovers a savings-balance lookup in a fictional bank UI through remote Playwright and saves a schema 2.0 capability. Replay follows the saved steps with typed inputs, a tenant binding, and a verified checkpoint. The demo contains three invented institutions and 25 invented customers. An authorized operator can take over a paused browser run and return it to automation.

The agent-facing HTTP catalog and FastMCP stdio server expose approved capabilities. Each invocation uses the unique name `name@artifact-id` and goes through the same tenant gateway and replay service.

## Setup

1. Install Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Run `uv sync --extra test` from this repository.
2. Copy `.env.example` to `.env`. Replace `BANKGPT_SIGNING_KEY` with a random value of at least 32 characters. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `MAIN_MODEL` for an OpenAI-compatible chat endpoint. The recorded model runs used `qwen2.5-7b-instruct-mlx` at `http://127.0.0.1:1234/v1`.
3. Run the supplied `docker-compose.yaml` on the browser machine. It serves Playwright 1.63.0 on port 8080. Replace the `192.168.x.xx` placeholder for `BANKGPT_PLAYWRIGHT_WS_URL` in `.env` with the browser machine's reachable address. The remote browser must reach the API machine's LAN URL, so set `BANKGPT_ALLOWED_ORIGINS` to that origin. Keep `BANKGPT_ARTIFACT_PUBLIC_ORIGIN` as `http://192.168.z.zz:8000` and `BANKGPT_PUBLIC_PLAYWRIGHT_HOST` as `192.168.x.xx`; saved bindings and evidence use these publication placeholders. The real addresses stay in ignored `.env`. Protect the unauthenticated browser port on a trusted LAN or VPN.
4. Start the API: `uv run uvicorn bank_gpt.api:app --host 0.0.0.0 --port 8000`. Confirm `http://127.0.0.1:8000/health` returns `{"status":"ok"}`. The fictional SQLite fixture is created at `data/mock_bank.sqlite3` on first import and is excluded from Git.

## Discovery, approval, and replay

With the API, model, and remote Playwright server running, these commands discover a new capability from the goal “Look up a member and read the current savings balance,” evaluate and approve its tenant bindings, then replay that exact base artifact:

```sh
uv run python scripts/run_shared_capability.py --model-discover --output /tmp/bank-gpt-discovery.json
BANKGPT_CAPABILITY_ID=$(python3 -c 'import json; print(json.load(open("/tmp/bank-gpt-discovery.json"))["base_capability_id"])')
uv run python scripts/run_shared_capability.py --existing "$BANKGPT_CAPABILITY_ID" --output /tmp/bank-gpt-replay.json
```

The discovery command uses the configured model to choose actions from observed controls. It saves an immutable base in `artifacts/`, creates separate tenant 1 and tenant 2 draft bindings, runs three fictional evaluations per binding, records a stability report, and approves each binding. Both commands assert successful replay with a checkpoint and tenant-scoped `member_not_found` results. Their JSON output scrubs member IDs and balances. The canonical reviewed base already in this repository is `d847b7f4-6460-433f-b53c-4f4640c2d352`.

For a path without a live model, omit `--model-discover`. The runner uses explicit decisions derived from the observed demo controls; it still needs the API and remote Playwright server. For checks without any live service, run `uv run --extra test pytest -q`.

## Other live demonstrations

Run these from the repository root with the API and remote Playwright server running. The fallback command also needs the configured model.

```sh
uv run python scripts/run_robustness.py --capability d847b7f4-6460-433f-b53c-4f4640c2d352
uv run python scripts/run_handoff.py --auto-operator
uv run python scripts/run_agent_mcp.py
uv run --extra test python scripts/run_codegen_demo.py
uv run python scripts/run_fallback.py
```

The robustness runner asserts 13 success, business, recovery, failure, and intervention cases. The handoff runner exercises the operator page. The FastMCP client launches `bank_gpt.mcp_server` over stdio, discovers catalog/invoke/status tools, calls an approved capability, and verifies the checkpoint. The code generator produces and runs a pytest snippet bound to an approved binding version; inputs and tokens come from its process environment. The fallback runner records one model-proposed locator correction, a rejected missing target, and a risky-control stop. Saved scrubbed traces are indexed in [evidence/README.md](evidence/README.md).

The FastMCP server may also be launched by a trusted stdio host with `uv run python -m bank_gpt.mcp_server`. Set `BANKGPT_API_URL` for its HTTP gateway and supply a short-lived `BANKGPT_IDENTITY_TOKEN` in the child environment. It obtains a fresh scoped replay grant for each invocation. Tokens do not appear in tool arguments.

## Documentation

- [REPORT.md](REPORT.md): design write-up under the assignment's seven headings.
- [docs/operation.md](docs/operation.md): operation, review workflow, and handoff.
- [docs/api.md](docs/api.md): HTTP and FastMCP contracts.
- [docs/artifact.md](docs/artifact.md): base capability and binding versions.
- [docs/architecture.md](docs/architecture.md): control flow and surface boundary.
- [docs/safety.md](docs/safety.md): policy and data-handling limits.
- [docs/tenant-security.md](docs/tenant-security.md): fictional identities and tenant authorization.
