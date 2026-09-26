# Architecture

A FastAPI process owns discovery, artifact replay, browser runs, tenant authorization, the approved catalog, and qualified invocation. It connects to remote Playwright; `192.168.x.xx:8080` is the publication placeholder for its address. Discovery asks the configured Qwen model for one structured action at a time; normal replay follows reviewed logical steps and tenant locators without model decisions. FastMCP runs as a separate stdio process and calls only the HTTP gateway for catalog, invocation, and read-only run status. The trusted host supplies an identity token in the child environment; the FastMCP process obtains a fresh scoped grant from the gateway for each invocation. This keeps membership, grant, and approval checks in one path. Browser handles and handoffs remain process-local; an API restart loses active runs.

# Artifact schema

Schema 2.0 separates an immutable base capability from tenant bindings. The base records the vendor product/version, typed inputs and outputs, ordered logical controls and actions, conditions, allowed actions, and final checkpoint. Binding versions record tenant entry route, origin, UI fingerprints, locator candidates, reviewed overrides, `draft`/`approved`/`revoked` state, reviewer, review time, and stability report. Each `vN` file is retained; an unversioned file points to the current state. Schema 1.1 is explicitly rejected. Example values, tokens, and customer results are absent from the artifact. The canonical base ID is `d847b7f4-6460-433f-b53c-4f4640c2d352`.

# Determinism & error handling

Replay checks tenant membership, a subject/tenant/base/operation scoped grant, binding approval, typed values, UI fingerprint, route and action policy, each unique visible target, declared conditions, and the final checkpoint. Business results such as `member_not_found` are distinct from hard failures such as `permission_denied`. Session expiry restarts once; a second expiry requests intervention. Missing and ambiguous targets pause with scrubbed observations. An operator may explicitly enable one model-proposed correction for a missing locator; the same policy and checkpoint checks apply, and the approved artifact is never silently changed. Live Qwen evidence covers accepted and rejected proposals.

# Heterogeneity & multi-tenant

`BrowserSurface` isolates Playwright operations from the artifact. A frameset or desktop adapter would need frame paths or accessibility/screenshot locators in a new schema version. The canonical base runs against North Harbor's `/demo` table UI and Pine Valley's `/demo/pine` account desk with different field names and balance markup. Both use the same typed contract and separate tenant bindings and grants. Three live draft evaluations per tenant produced 100% stability reports and explicit approval versions. The catalog filters to approved bindings and names entries as `name@artifact-id`, so duplicate descriptive names remain unambiguous.

# Escalation & handoff

Discovery and replay pause for uncertainty, policy stops, verification, missing targets, and exhausted recovery. The intervention records reason, step, scrubbed observation, owner, and a non-secret browser-session ID. An authorized operator acts on the same page and resumes the run; the operator ID and action are logged. A reader or another tenant's operator cannot take over. A process restart loses the held page and returns 404 for the old run. Live pause, action, resume, repeated handoff, and restart traces are in `evidence/handoff/`.

# Safety

The demo uses invented records. SQLite membership and RBAC are rechecked at gateway authorization and demo UI requests. Identity, browser-session, and `secure_permissions` JWTs have distinct audiences and token types; grants last two minutes and bind subject, tenant, base, and operation. Routes, actions, destinations, network requests, risky controls, and unique target matches are checked before automation acts. Observations and saved evidence scrub known inputs, long numbers, amounts, and tokens; the authorized caller still receives the requested raw output. FastMCP has no direct customer-table or raw browser access. Remaining limits include heuristic redaction, an unauthenticated remote Playwright service on the trusted LAN, a local fixture identity issuer, and process-local run state.

The ignored `.env` holds the real LAN addresses. Published artifacts and evidence use `192.168.x.xx` for the browser host and `192.168.z.zz` for the API host. Binding load resolves the API placeholder from configuration before policy checks and replay.

# Cuts

The code generator emits a runnable pytest snippet from a base ID and immutable tenant binding version. It reads tokens and fictional input values from environment variables, asserts declared outputs and checkpoint, and fails if the binding version changed. Live evidence also covers the 13-case robustness matrix, two-tenant reuse, human handoff, qualified agent invocation through FastMCP, code generation, three-run stability review per tenant, and assisted fallback. Durable run and lease storage, a production identity provider and vault, complete financial-data classification, and a real desktop adapter were left out to keep the demonstrated browser path small and reviewable. Those are the next steps for an environment beyond this fictional fixture.
