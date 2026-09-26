# Evidence index

This index is useful because the repository contains historical schema 1.1 traces, current schema 2.0 traces, and several independent live checks. JSON traces contain scrubbed observations rather than raw customer inputs, balances, passwords, or tokens. Network addresses in this repository are publication placeholders; they do not reproduce the exact addresses used during capture.

## Historical discovery (schema 1.1)

`capability.json` is a **historical tenant-bound schema 1.1 artifact**, with its associated `discovery-run.json`, `replay-success.json`, `replay-not-found.json`, and `tenant-denials.json` traces. These show the early model-driven discovery, deterministic replay, `member_not_found` result, and cross-tenant HTTP 403 responses. Schema 1.1 is rejected by the current API and is retained only to document the earlier implementation.

## Current shared capability (schema 2.0)

`shared-capability/model-live-reuse.json` records model-driven discovery of base `d847b7f4-6460-433f-b53c-4f4640c2d352`, replay on two different tenant UIs, tenant-scoped not-found results, and an isolated locator-drift check. The runner registers and approves the two tenant bindings; their current approved versions are in `artifacts/`. `shared-capability/live-reuse.json` records a run using explicit decisions derived from observed controls. `shared-capability/live-version-check.json` records replay and drift checks against a separate approved base. `shared-capability/post-integration-model.json` records a later model-driven discovery of base `31380f6b-572e-49c9-9ac9-00bbc339de8c` and replay in both tenants.

`stability/review.json` contains three evaluated draft runs per tenant, their success rates, and approval versions for the canonical base. `agent-mcp/live-invocation.json` records FastMCP stdio tool discovery, a denied cross-tenant catalog request, approved qualified-name invocation, and read-only run status. `codegen/` contains generator input metadata, a version-bound pytest snippet, and its live verification result. `fallback/live-qwen.json` records an accepted locator correction, a rejected proposal, and a risky-control stop.

## Robustness and handoff

`robustness/` has 13 asserted live scenarios against the canonical schema 2.0 base `d847b7f4-6460-433f-b53c-4f4640c2d352`. Each case contains a manifest, scrubbed result, events, and an observation or no-failure marker. The runner checks each expected status and code, plus case-specific checkpoint, recovery, failure-observation, or policy events, then closes the run. These traces include validation, session recovery and exhaustion, delays, application and permission errors, missing and ambiguous targets, disallowed destinations, risky controls, tenant membership, and success.

`handoff/verification-trace.json` shows a verification pause, reader and cross-tenant operator denials, rejected premature completion, an operator action on the held page, and success after resume. `handoff/completed-step-trace.json` shows explicit completion of a paused Search step. `handoff/restart-paused.json` and `handoff/restart-check.json` show that a paused run returns HTTP 404 after API restart, documenting the process-local state limit.

`integration/verification.json` indexes the recorded test and live demonstration results. It records the checks performed at capture time; it is not a claim that the live browser, model, or API is currently available.
