# Hosted Agent (Phase 19.1e)

The Phase 18 controlled agent, packaged so **Microsoft Foundry hosts the
process** — without rewriting its control model.

The Responses protocol is served by the official
**`azure-ai-agentserver-responses`** library. This package no longer imitates
it: the SSE lifecycle, response ids, `/responses` routing, cancellation, the
response store and `/readiness` all belong to `ResponsesAgentServerHost`. What
remains here is a handler and two pure rendering functions.

```
Foundry                         hosts, scales, routes, identity, PORT
    |
    v  POST /responses
ResponsesAgentServerHost        protocol, SSE, store, readiness   (library)
    |
    v  @host.response_handler
hosted_agent.server             translation ONLY
    |
    v  AgentService.run()
Phase 18                        registry, policy, bounded loop, approval, audit
```

## Why this is a separate package

`labs/agent-ecosystem/foundry-agent` needs `azure-ai-projects`, which requires
`openai>=3`. The product pins `openai>=1.99,<3`. One resolution cannot hold both.

A **hosted** agent is called *into* — it needs no Foundry client SDK at all — so
this package can take a proper path dependency on the product instead of the
source-path bootstrap the other lab is forced into. Deployment tooling, which
*does* need the SDK, stays on the other side in
`foundry_agent_lab.hosted_deploy`.

It is deliberately **not** a uv workspace member, for the same reason.

## A packaging constraint worth knowing

The product resolves `PRODUCT_ROOT` from its own `__file__` and reads its
retrieval config, prompts and corpus manifest from the repository beside it.
None of that ships in a wheel, so installed non-editable it lands in
`site-packages` and `build_service` fails with *"Retrieval configuration could
not be read"*.

So the dependency is **editable**, and the container image `COPY`s the product
source and syncs there. Making the product a self-contained distributable is
real work and is a prerequisite for any hosting route that does not carry the
repository — it is not done here.

## Identity, hosting, scaling, state and version ownership

| Concern | Owner | Detail |
|---|---|---|
| Compute, scaling, routing | **Foundry** | `cpu` / `memory` in the definition; Foundry supplies `PORT` |
| Process identity | **Foundry** | Managed identity injected; the container reads no key |
| Azure OpenAI auth | **Application** | The product's existing `DefaultAzureCredential` path |
| Agent name + version | **Foundry** | `agents.create_version*` — immutable versions |
| Container image / code zip | **Application** | Built and hashed here; Foundry runs it |
| Conversation / session state | **Foundry** | Session operations; the agent itself is stateless per turn |
| Corpus, retrieval, prompts | **Application** | Versioned in the repository, carried in the image |
| Tool registry + risk | **Application** | Never sent to Foundry, never read back |
| Policy | **Application** | `authorise()` runs in-process |
| Approval record + decision | **Application** | Served from this process at `/v1/approvals` |
| Audit trajectory | **Application** | The product's sink, configured by env |
| Readiness gating | **Shared** | The process reports it; Foundry acts on it |

The line that matters: **Foundry owns where the process runs. The application
owns what it is allowed to do.** A state-changing proposal still stops for a
human, and the approval lives here — a test asserts it end to end through the
protocol surface.

## Hosted Agent vs Prompt Agent

| | Prompt Agent (19.1b–d) | Hosted Agent (19.1e) |
|---|---|---|
| Where the loop runs | Foundry | **your process** |
| Who dispatches tools | your code, between turns | your code, in-process |
| Tool schema | sent to Foundry | not sent — internal to the agent |
| Model choice | named in the definition | your code calls the model |
| Deployable artefact | none | container image or code zip |
| Cold start / scaling | Foundry's, invisible | Foundry's, but of *your* image |
| Dependency freedom | none — Foundry runs the model | full — any library you package |
| Grounding enforcement | **lost** — Foundry composes the answer | **kept** — composition is in-process |
| Ops burden | lowest | image builds, base-image CVEs, sync |
| Blast radius of a bad version | prompt/tool contract | the whole process |

The decisive difference is the grounding row. 19.1b recorded that a Prompt Agent
composes the final answer inside Foundry, where the application cannot check it
— so Phase 18's server-side citation rebuilding and fail-closed refusal do not
survive. **The Hosted route gets them back**, because the model call and the
grounding enforcement both happen in the process you shipped.

The price is that you now own an image: base-image patching, dependency drift
and a slower deploy loop. Prompt Agents are the right default for orchestration
you are happy to delegate; Hosted is what you reach for when a control must run
inside the same process as the model call.

## Local validation

```bash
./labs/agent-ecosystem/hosted-agent/validate.sh
```

Format, lint, strict types, tests, an in-process turn through the real
`ResponsesAgentServerHost`, and — when a Docker daemon is available — the
container check below. No Azure, no deployment.

### The container check

```bash
./labs/agent-ecosystem/hosted-agent/scripts/container_smoke.sh
```

Builds the image for `linux/amd64`, asserts it runs unprivileged as **uid
10001**, asserts the **approved corpus and every document it references** are
present under the image's repository root, starts it under its **declared CMD**
(not an overriding command), supplies `PORT` the way the platform does, requires
the process to stay alive, polls `/readiness` until 200, and fails if an
import/module error reaches the container's output.

Each of those is a property that fails at the *first Foundry session* rather than
at build time, which is why it is asserted against the artefact instead of
trusted to a `COPY` or a `USER` line.

Run it before building any image you intend to push. Everything else in
`validate.sh` runs against this machine's virtualenv, where `hosted_agent` is
importable by construction — which is precisely how a broken image passed
validation, was pushed, was deployed as version 1, and then failed every Foundry
session with:

```
/app/labs/agent-ecosystem/hosted-agent/.venv/bin/python: No module named hosted_agent
```

### Why version 1 failed (fixed)

Two faults, both of the same kind: **the image was not a faithful copy of the
environment the package needs.**

1. **Dangling editable installs.** The product and this package are both
   installed editable — the product must be, because it resolves `PRODUCT_ROOT`
   from its own `__file__` and reads its config, prompts and corpus manifest
   from the repository beside it. An editable install is recorded as a `.pth`
   file holding an **absolute** path. The build stage synced under `/build`; the
   runtime stage copied the tree to `/app`. The `.pth` files still said
   `/build/...`, which no longer existed, so the distribution was installed and
   the module was unimportable. The build and runtime stages now share one path,
   `/app`.
2. **No corpus.** `corpus/manifest.json` names its documents repository-relative
   under `docs/`, and the loader derives the containment root from a `.git`
   entry. Neither was in the image, so once the import was fixed the process
   exited 3 with `category=corpus`. The image now carries `docs/` and declares
   `/app` as its containment root.

The image also no longer bakes `ENV PORT`; see the port contract below. A `RUN`
in the runtime stage now imports `hosted_agent` as the unprivileged user, so a
build that cannot import its own entry point produces no taggable image.

### The port contract

`azure.ai.agentserver.core._config.resolve_port` resolves the bind port from the
**`PORT`** environment variable, defaulting to `8088`; `run()` binds `0.0.0.0`
by default. `hosted_agent.config` reads the same variable with the same default
and passes the result explicitly, so the two cannot disagree.

**The manifest must not declare `PORT`** — Foundry reserves it and injects it —
and it does not. The image does not set it either. `EXPOSE 8088` documents the
default only; the `HEALTHCHECK` reads `PORT` rather than hardcoding a port.

### The resource-tier contract

`cpu` and `memory` are **one choice, not two.** Foundry accepts a fixed set of
tiers and rejects every other combination:

| `cpu` | `memory` |
|---|---|
| `0.25` | `0.5Gi` |
| `0.5` | `1Gi` |
| **`1`** | **`2Gi`** ← this agent |
| `2` | `4Gi` |

`1` with `4Gi` is not a tier, and neither is `2` with `2Gi`, even though every
one of those four values appears somewhere in the table. Validating the fields
independently is exactly how an unsupported pairing was sent and refused;
`hosted_deploy.RESOURCE_TIERS` now holds the constraint as pairs and
`load_manifest` rejects anything that is not one of them, naming the whole table
in the refusal.

Moving up a tier means moving both fields together.

## Deployment

**Version 2 is deployed and working.** The steps below are the ones that
were run, in the order they must be run.

### The order is not negotiable

```
create version  ->  read runtime identity  ->  grant inference role  ->  invoke
```

Foundry mints the agent's **runtime managed identity when the version is
created**. It does not exist before that, so the RBAC it needs cannot be
expressed in the Terraform that builds the account — there is no principal to
grant to, and no data source that can resolve one for a version nobody has
created yet. See `scripts/grant_runtime_role.sh` and the section on
runtime-identity RBAC below.

The trap this ordering closes: **skipping the grant does not fail the
deployment.** The version reaches `active`, the container starts, `/readiness`
returns 200 — and then every turn fails, because the model call is rejected.
An agent can be perfectly healthy and still unable to do anything.

### 1. Apply the Terraform that creates the registry

```bash
cd infrastructure/capabilities/ai-foundry/sandbox

terraform init
terraform plan -out=tfplan \
  -var="subscription_id=$(az account show --query id -o tsv)" \
  -var='allowed_ip_cidrs=["<YOUR_EGRESS_IP>/32"]'
terraform apply tfplan
```

Adds a Basic-SKU ACR with the admin account disabled, and grants the Foundry
**project** managed identity a read-only pull role scoped to that registry.
Nothing else in the capability changes: no second Foundry account, no second
project, no second model deployment.

```bash
terraform output container_registry_login_server
```

### 2. Build and push the image

Built from the repository root, because the product is a path dependency.
`--platform linux/amd64` is not optional — Foundry runs amd64 nodes, and an
arm64 image pushes happily and then fails at start with an exec format error.

**Validate the image first.** Version 1 was pushed without ever having been
started, which is why it failed in Foundry rather than on the workstation.

```bash
./labs/agent-ecosystem/hosted-agent/scripts/container_smoke.sh
```

Then build and push an **immutable** tag. `git rev-parse HEAD` alone is not
immutable if the tree is dirty, so the tag records the commit *and* refuses to
be built from uncommitted work:

```bash
test -z "$(git status --porcelain)" || { echo "commit first: the tag names a commit"; false; }

LOGIN_SERVER=$(terraform -chdir=infrastructure/capabilities/ai-foundry/sandbox \
  output -raw container_registry_login_server)
TAG="phase19-1e-$(git rev-parse --short=12 HEAD)"
IMAGE="$LOGIN_SERVER/phase19-hosted-controlled-agent:$TAG"

# --pull so the base image is today's patched python:3.12-slim, not a stale
# local layer. --provenance=false because ACR + Foundry want a plain image
# manifest, not an OCI index.
docker build --platform linux/amd64 --pull --provenance=false \
  -f labs/agent-ecosystem/hosted-agent/Dockerfile \
  -t "$IMAGE" .

# The same assertion the smoke test makes, against the tagged artefact.
#
# NOTE THE EXPLICIT `python`. The image declares CMD and NO ENTRYPOINT, so an
# argument list REPLACES the command rather than being appended to it:
# `docker run "$IMAGE" -c "import hosted_agent"` tries to execute a binary
# named `-c` and fails with an exec error that looks nothing like an import
# failure. The interpreter has to be named.
docker run --rm --platform linux/amd64 "$IMAGE" \
  python -c "import hosted_agent; print(hosted_agent.__file__)"

az acr login --name "${LOGIN_SERVER%%.*}"
docker push "$IMAGE"

# Record the digest. A tag can be moved; a digest cannot.
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
echo "$IMAGE"
```

`az acr login` uses your Entra identity. The registry has no admin account, so
there is no username/password to store anywhere.

### 3. Update the manifest

Replace the two placeholders in `agent.manifest.json`:

```jsonc
"image": "<LOGIN_SERVER>/phase19-hosted-controlled-agent:<TAG>",
"AZURE_OPENAI_ENDPOINT": "https://aif-example-sandbox.openai.azure.com/openai/v1/"
```

That is the **model data-plane** endpoint, not the project endpoint.
`registry_connection_id` stays `null`: for an ACR in the same subscription
reached by managed identity, the standard path needs no connection object.

Review the result without contacting Azure:

```bash
uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.hosted_deploy --plan
```

### 4. Create the Hosted Agent version

**A version is immutable.** Version 1 carries the broken image and cannot be
repaired in place — the fix ships as **version 2**, built from the new tag.
Creating a version is an externally consequential action and needs human
approval; nothing below has been run.

```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from foundry_agent_lab.hosted_deploy import build_definition, load_manifest

manifest = load_manifest()
client = AIProjectClient(
    endpoint="https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab",
    credential=DefaultAzureCredential(),
)

# The manifest must already name the NEW tag, or this creates a second broken
# version. Assert it rather than trusting the edit.
assert manifest.container_image and manifest.container_image.endswith(TAG), manifest.container_image

version = client.agents.create_version(
    agent_name=manifest.agent_name, definition=build_definition(manifest)
)
print(version.name, getattr(version, "version", None))
```

Version 2 is live once its deployment status reaches `active`. Read-only, no
approval needed:

```python
for v in client.agents.list_versions(agent_name="phase19-hosted-controlled-agent"):
    print(
        v.version, getattr(v, "deployment_status", None), v.definition.container_configuration.image
    )
```

### 5. Grant the runtime identity its inference role

```bash
export FOUNDRY_PROJECT_ENDPOINT="$(terraform \
  -chdir=infrastructure/capabilities/ai-foundry/sandbox output -raw llm_project_endpoint)"

./scripts/grant_runtime_role.sh phase19-hosted-controlled-agent 2            # prints, does not grant
./scripts/grant_runtime_role.sh phase19-hosted-controlled-agent 2 --apply    # needs approval
```

Idempotent, and it resolves the principal from the live version every time. See
the runtime-identity section below for why this cannot be Terraform.

Data-plane role propagation takes **several minutes**. An immediate retry after
granting will still fail; that is propagation, not a misconfiguration.

### 6. Smoke-test the deployed agent

These are the exact prompts that were run against version 2, with the metadata
they returned.

**Read-only** (no approval needed) — a grounded answer over the approved corpus:

```python
openai_client = client.get_openai_client(agent_name="phase19-hosted-controlled-agent")

r = openai_client.responses.create(input="How is Terraform state separated between environments?")
print(r.output_text, r.metadata)
```

Observed:

| field | value |
|---|---|
| `outcome` | `answered` |
| `policy_decision` | `allow` |
| `selected_tool` | `search_platform_docs` |
| `tool_risk_level` | `read_only` |
| `tool_execution_status` | `succeeded` |
| `tool_iterations` | `2` |
| `citation_count` | `4` |

**Approval-required** — the control this whole route exists to prove. The agent
must refuse to act and hand back an application-held approval id:

```python
r = openai_client.responses.create(
    input=(
        "I want to formally propose a platform change for review - "
        "raise sandbox Foundry capacity to 30."
    )
)
assert r.metadata["outcome"] == "approval_required"
assert r.metadata["tool_execution_status"] == "not_executed"
assert r.metadata["approval_id"]
```

Observed:

| field | value |
|---|---|
| `outcome` | `approval_required` |
| `policy_decision` | `require_approval` |
| `selected_tool` | `propose_change_request` |
| `tool_risk_level` | `state_changing` |
| `tool_execution_status` | `not_executed` |
| `approval_id` | `apr-…` (application-held) |

The text says, in words, that the action was **not** carried out. The approval
record lives in the application's `ApprovalService`, not in Foundry.

**Negative control** — a direct request to apply Terraform:

```python
r = openai_client.responses.create(input="Apply the Terraform for the sandbox environment now.")
assert r.metadata["outcome"] == "refused"
assert r.metadata["tool_execution_status"] == "not_executed"
```

Observed `outcome: refused`, `refusal_reason: out_of_scope`,
`tool_execution_status: not_executed`. The agent has no such tool, and says so
rather than improvising.

#### Diagnosing a failing session

`client.get_openai_client(...)` returns an **`openai`** client, not an Azure SDK
client. Its request path raises **`openai.APIStatusError`** (and its subclasses
`APIConnectionError`, `APITimeoutError`) — *not*
`azure.core.exceptions.HttpResponseError`. Catching the Azure exception here
catches nothing: the failure propagates uncaught and the readiness detail is
lost.

The 424 that version 1 returned on every call is a **session readiness**
failure, not an agent error, so the useful detail is on the HTTP response:

```python
import json

import openai

try:
    r = openai_client.responses.create(input="ping")
    print(r.output_text, r.metadata)
except openai.APIStatusError as error:
    # 424 = the session's container never became ready. The cause is in the
    # container's own log, not in this body — but the body names the session.
    print("status:", error.status_code)
    print("request id:", error.response.headers.get("x-request-id"))
    print("body:", json.dumps(getattr(error, "body", None), indent=2, default=str))
except openai.APIConnectionError as error:
    print("no response was received:", error)
```

A 424 means the container exited or never bound the port. Fetch the session log
from Foundry and read the FIRST error, not the last: the process exits fast, so
the tail is shutdown noise. `No module named ...` or `Agent could not be built:
category=...` are both packaging faults — reproduce them with
`scripts/container_smoke.sh` rather than by redeploying.

## Failure taxonomy: what the caller sees, and why a 500 can still happen

Three layers can fail, and only two of them are ours.

| Layer | Failure | What the caller gets |
|---|---|---|
| The agent | classified (`AssistantError`) | **200**, `outcome: failed`, named `failure_category` |
| The adapter | an unexpected defect | **200**, `outcome: failed`, `failure_category: internal_error`, `request_id` |
| The platform | the official server cannot complete the response | **500**, `{"code": "server_error"}` — not ours to catch |

**Known failures stay structured.** The product classifies every provider and
configuration fault — 401 → `authentication`, 403 → `authorization` or
`network_denied`, 404 → `configuration`, 429 → `rate_limited` — and
`AgentService.run` raises only `AssistantError`. The handler maps that to a
completed response with a named category, so a caller branches on data rather
than parsing prose.

**Unexpected defects stay observable without leaking.** Anything that is *not*
an `AssistantError` is a bug. It is caught, `logger.exception` writes the
traceback **to the container log**, and the caller receives only
`failure_category: internal_error` plus the `request_id` to quote. A traceback
carries file paths and an exception message can quote the request, the corpus or
a principal; none of that crosses the boundary. Tests assert both halves.

### Why an application-controlled failure can still surface as an outer 500

This is the part the adapter cannot fix, and it is what made the 19.1e 500s
confusing.

`azure-ai-agentserver-responses` does more than call the handler. It owns the
response lifecycle, and with `store=true` it **persists the response to
Foundry's response storage** after the handler returns. That persistence is an
outbound call from the container to a Foundry endpoint.

So the sequence during the firewall investigation was:

1. the handler ran and returned a perfectly well-formed `TextResponse` —
   application-controlled, structured, a 200 as far as our code was concerned;
2. the server then tried to store it;
3. the storage call was **blocked by the account firewall**;
4. the server failed *after* our handler had already succeeded, and rendered a
   generic `500 {"code": "server_error"}`.

**A 500 is therefore not evidence that the agent failed.** It can mean the agent
succeeded and the platform could not record the result. Two outbound paths must
both be reachable — the model data plane (`/openai/v1/responses`) and Foundry
response storage — and blocking either produces the same opaque 500. When you
see one, check egress before you suspect the handler; the handler's own failures
arrive as a 200 with a category, by construction.

## Runtime-identity RBAC cannot be known in advance

The agent's runtime managed identity is created **by Foundry, when the version
is created**, and reported as `instance_identity.principal_id`. Before that
moment there is no principal, no object id, and nothing for a role assignment to
reference.

That is why the inference role is **not** in Terraform. It is not an oversight
and it is not deferred work: an apply that runs before the version exists has
nothing to grant to, and one that runs after would need the version id threaded
into state, coupling infrastructure to a deployment artefact it should not know
about. `scripts/grant_runtime_role.sh` closes the gap instead — it resolves the
principal from the live version, checks the existing assignments, and does
nothing if the role is already there.

**The live principal is deliberately not committed anywhere.** A principal id is
per-version and per-environment; hard-coding one would record a fact with an
expiry date and, worse, would keep granting to an identity that a rebuilt agent
no longer uses.

What was granted, for the record: **`Cognitive Services OpenAI User`**, scoped
to the Foundry **account** (not the subscription), to the version 2 runtime
principal. Owner and Contributor are management-plane roles and grant none of
the data-plane actions an inference call needs.

## Network findings — the live proof required a temporary relaxation

**This is not a working default-deny configuration, and this README does not
claim it is.**

The Foundry account normally runs `publicNetworkAccess: Enabled`,
`defaultAction: Deny`, `bypass: None`, with an IP allowlist for operator
workstations. Under that configuration the Hosted Agent **cannot work**, and the
live smoke tests above would not have passed.

What was established:

1. Granting `Cognitive Services OpenAI User` moved the model response from
   **401 to 403** — proof that RBAC became effective, and that the remaining
   obstacle was the network, not the identity.
2. `bypass: AzureServices` did **not** admit Hosted Agent session traffic. It is
   deliberately **not** added to Terraform: it widens the account's exposure and
   did not solve this path, which is the worst combination available.
3. Two outbound calls were firewall-blocked, not one: the Azure OpenAI
   `/openai/v1/responses` data plane, and the official Agent Server's Foundry
   response storage.
4. For the smoke tests only, `defaultAction` was temporarily set to `Allow` and
   **immediately restored to `Deny`** afterwards. The account is default-deny
   again now.

So the honest statement of what was proven: **the agent, the control model and
the packaging are correct, and were demonstrated end to end during a
time-boxed, sandbox-only network relaxation that has been rolled back.** The
network path is not solved.

### Follow-up: private networking is a create-time prerequisite

Production-grade Hosted Agent networking requires **VNet injection configured
when the Foundry account is created**. Microsoft does not support adding it to
an existing account afterwards, so this cannot be retrofitted onto
`aif-example-sandbox` — it requires a new account and therefore a new
capability root.

Recorded as a **future infrastructure prerequisite, not work completed in Phase
19.1e.** Any environment that needs a Hosted Agent to run under default-deny
must design the account for VNet injection up front.

## Status

**Version 2 is deployed, active and working.** The protocol is served by
`azure-ai-agentserver-responses`; the handler contract, the `TextResponse`
`configure` hook and the `/readiness` route were introspected from the installed
library rather than taken from documentation.

| | |
|---|---|
| agent | `phase19-hosted-controlled-agent` |
| version | **2** (active) — 1 CPU / 2Gi, `responses` 1.0.0 |
| image | `…azurecr.io/phase19-hosted-controlled-agent:phase19-1e-222a28e89da6` |
| digest | `sha256:b00c8e25…96d937` |

Version 1 reached `active` and then failed **every** session with HTTP 424: the
container could not import its own entry point, and separately carried no
corpus. Both causes are diagnosed above and fixed; `scripts/container_smoke.sh`
is the regression guard, and it runs the artefact rather than this workstation.

Proven end to end: grounded read-only answers with citations; the
approval-required control holding a `state_changing` tool at
`not_executed` with an application-held `approval_id`; and an out-of-scope
refusal. Evidence and exact prompts are in step 6.

### What is NOT proven

- **The default-deny network path.** The live proof required a temporary,
  sandbox-only firewall relaxation that has been rolled back. See the network
  findings above. VNet injection is a create-time prerequisite and is follow-up
  work.
- **Bit-identical reproducibility of the running image.** The deployed digest
  was built before this branch's final hardening — the `TARGETARCH` assertion
  and the adapter's unexpected-defect fallback landed after version 2 was
  created. Rebuilding at this commit produces a **different digest** with the
  same functional packaging (paths, corpus, non-root uid 10001, platform port).
  The next version built from this branch will carry both improvements; no
  redeploy was performed for them.
- **Any externally consequential action.** `propose_change_request` still
  performs no mutation.

## Deliberately not here

No multi-agent system. No new business tools. No real external write —
`propose_change_request` still performs no mutation. No general observability
infrastructure.
