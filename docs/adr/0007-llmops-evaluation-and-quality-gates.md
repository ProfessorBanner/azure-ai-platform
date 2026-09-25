# ADR 0007: Evaluation, adversarial testing and LLMOps quality gates

## Status

Accepted. Introduced by Phase 17.2 for `products/platform-engineering-assistant`.

Not added to the assistant's own corpus (`corpus/manifest.json`). Corpus
membership is out of scope for the phase that wrote this document, and a
document describing how the product is evaluated is not documentation the
product needs to answer questions about the platform.

## Context

Phase 17.1 built the assistant: an approved corpus, BM25 retrieval, a versioned
prompt, an Azure OpenAI provider, fail-closed grounding enforcement and a
FastAPI surface. It measured RETRIEVAL offline and deliberately measured nothing
about generation.

That left a specific, named gap. Grounding enforcement proves **citation
containment**: every citation names a chunk that was actually retrieved. It
proves nothing about whether the cited chunk *supports* the sentence attached to
it. A model can cite a real chunk and still assert something the chunk does not
license, and that is the failure a reader is least able to detect — the citation
is precisely what makes the claim credible.

Phase 17.2 closes that gap far enough to gate changes on it, without pretending
to have closed it completely.

## Decision

### 1. One answer path, never two

The evaluation runner calls `AnsweringService.answer` — the same method, on the
same object, that the FastAPI route calls. There is no evaluation-only
retrieval, no evaluation-only prompt assembly and no evaluation-only grounding.

A parallel path would measure the parallel path. The suite would go green while
served behaviour drifted, which makes an evaluation harness worse than none: it
converts an unknown into a false assurance.

### 2. A versioned golden dataset of sixteen cases, with recorded provenance

`evaluation/generation_v1.json`: 8 answerable, 3 out-of-scope, 2
insufficient-evidence, 2 prompt-injection, 1 authority-conflict. Every case
records a stable id, expected disposition, expected documents or allowed refusal
reasons, prohibited substrings, category, tags, evidence path and section, and a
written rationale.

The file states in its own provenance block that it is **not an independent
benchmark**: the same engineering process wrote the corpus, the retriever, the
prompt and these questions, so a good score is evidence that the pipeline behaves
as designed, not that it generalises.

It also records a **measured retrieval ceiling**. Nine of the eleven expected
documents are reachable within `top_k = 6`, an 81.8% ceiling on citation recall.
The two misses are recorded and deliberately not rewritten, because rewriting a
question until it passes is fitting the instrument to the result. One of them is
a finding worth keeping: the injection preamble in GEN-X02 dominates the lexical
query and pushes the correct ADR out of the retrieved set entirely.

### 3. Deterministic metrics are load-bearing; the judge is an estimate

Deterministic evaluators decide the gate. They are pure functions over recorded
outcomes: schema validity, disposition accuracy, refusal accuracy, answerable-case
accuracy, citation containment, expected-document citation recall, prohibited
content, provider failures, and latency and token distributions.

**`citation_containment_rate` is never called groundedness**, anywhere, and a
test enforces that. Conflating a cheap structural check with a correctness
guarantee is the most consequential wrong claim this codebase could make about
itself.

The semantic judge is optional, live-only, and produces a strict Pydantic
verdict against a versioned rubric (`prompts/judge_v1.md`). It sees the question,
the response, the cited evidence and the rubric — and nothing else. There is no
field on `JudgeRequest` for an expected disposition, expected documents, a case
id or a score, so no future caller can pass it an answer key.

### 4. Quality gates are live-only, and that is stated rather than hidden

The deterministic fake provider is not a model: it answers with a fixed string
and cites by rule. Its disposition accuracy measures the stub.

Gating offline runs on quality would therefore either fail every CI run or force
the fake to be taught the right answers — which is manufacturing a passing score.
So the structural gates (schema validity, containment, prohibited content,
provider failures) apply in both modes, and the quality, latency and token gates
apply only against a real deployment. Quality metrics are still **computed and
reported** offline; ungated is not unmeasured.

### 5. A missing metric reports INCOMPLETE, never PASS

Gates have three outcomes. If a required metric could not be computed — no judge
ran, no tokens were reported, the run aborted — the honest verdict is "unknown",
and collapsing that into PASS is how a suite silently stops testing anything.
FAIL dominates INCOMPLETE, because a definite breach is information and a missing
metric is the absence of it.

A **live** report produced from a dirty or unidentifiable working tree is forced
to INCOMPLETE and marked prominently: it cannot be attributed to a commit, so it
is not reproducible and must never become a baseline.

### 6. Thresholds live in a versioned, server-owned policy

`evaluation/evaluation_policy_v1.json`. Not code, not an environment variable,
not a flag. Changing what counts as acceptable must appear in a diff.

Every threshold carries a recorded rationale, and the file states plainly that
these are **initial engineering gates** chosen from the shape of a sixteen-case
set with no production traffic and no human labels, which must be recalibrated
before a pass is read as evidence of quality.

### 7. Regression comparison diffs cases, not percentages

Two runs can both report 87.5% accuracy while disagreeing about which cases
failed. Aggregates hide the substitution of one failure for another, which is the
signature of a behaviour change rather than of noise. The comparison command
therefore diffs dispositions, pass/fail, unsupported-claim counts and cited
documents per case, then reports prompt/model/configuration changes and cost
regressions.

**No baseline is shipped.** The first live report accepted after human review
becomes the baseline, chosen by a person. A manufactured baseline would give
every future comparison a fictional reference point that reads exactly like a
real one.

### 8. Reports are output, not source

JSON and Markdown, written to a gitignored `artifacts/evaluation/` directory.
Redaction is structural: `CaseOutcome` has no field for a question, an answer, a
context block, a chunk body or the judge's rationale, so no report can print
them. Live model responses are never committed.

### 9. Foundry evaluation integration is DEFERRED, not faked

See "Foundry evaluation integration" below.

### 10. CI runs the offline evaluation and never calls a model

`azure-pipelines/platform-engineering-assistant-ci.yml` runs lock validation,
formatting, lint, strict mypy, unit tests and the deterministic offline
evaluation, and publishes the report. It uses no service connection and no
credential. Live evaluation remains a manual, human-invoked, cost-controlled
command.

The pipeline is added as a file and is **not registered or queued** by this
change.

## Foundry evaluation integration

The requirement was to add a small optional adapter **only if** the currently
supported Microsoft Foundry SDK can associate an evaluation run with the existing
project `proj-capability-lab` using `DefaultAzureCredential` and
`FOUNDRY_PROJECT_ENDPOINT`, with no API keys, no new Azure resources and no
automatic execution.

**Decision: defer the adapter.** The blocker is concrete and verifiable, and
faking the integration or adding an unstable dependency to claim support would be
worse than not having it.

### The exact limitation

1. **A hard dependency conflict on the live generation path.** The current
   supported route to Foundry evaluation is
   `AIProjectClient.get_openai_client().evals.*` — the OpenAI Evals surface
   proxied through the project. Package metadata from PyPI, read while writing
   this ADR:

   | `azure-ai-projects` | required `openai` |
   | --- | --- |
   | 2.0.0 – 2.4.0 | `>= 2.8.0` |
   | 2.5.0 (latest) | `>= 3.0.0` |

   This product pins `openai>=1.99,<3`, and its Responses adapter
   (`generation/azure_openai.py`) is written and locked against that major
   version. Adopting the SDK would force a major-version bump of the SDK on the
   **live answering path** inside a batch whose scope is evaluation. That is a
   change with its own risk, its own testing and its own decision — not a side
   effect of adding reporting.

2. **The remaining Foundry-native evaluation surfaces are vendor-labelled
   preview.** `.evaluation_rules` is available, but `.beta.evaluators`,
   `.beta.evaluation_taxonomies`, `.beta.insights`, `.beta.schedules` and
   `.beta.red_teams` are all published under `beta`. Building a quality gate on a
   preview API means the gate's availability is outside this repository's
   control.

3. **The superseded 1.0.0 line needs infrastructure this platform deliberately
   does not have.** Its `.evaluations` operations take a `Dataset`, uploaded via
   `datasets.upload_file`, which requires a connected Azure Storage account on
   the project. The Foundry capability root provisions none — ADR 0006 chose the
   modern resource model specifically to avoid dragging a Key Vault and a storage
   account into scope — and "no new Azure resources" is a hard constraint here.

4. **`azure-ai-evaluation` is not a lighter alternative.** Version 1.18.3 pulls
   `pandas`, `nltk`, `aiohttp`, `msrest`, `ruamel.yaml`, `Jinja2` and
   `azure-storage-blob` into a product whose entire runtime dependency set is
   five packages, and its project-logging path also writes results to blob
   storage.

### What would unblock it

Any one of: a Foundry evaluation-logging API that is GA and does not require a
storage connection; an `azure-ai-projects` release compatible with `openai<3`;
or a separate, reviewed decision to move the product's generation adapter to
`openai>=3`. None of those is in scope for Phase 17.2.

Local evaluation has no dependency on Foundry and never will: the runner, the
metrics, the gates and the reports are all product-local, and that independence
is deliberate.

## Consequences

### Positive

- Changes to the prompt, the retriever or the model can be gated on measured,
  versioned behaviour instead of on inspection.
- Every reported number is attributable to a commit, a corpus, a retrieval
  configuration, a prompt, a rubric, a dataset and a policy — all hashed.
- Adversarial behaviour is covered by cases, not by hope: injection, prompt
  disclosure, credential disclosure, instruction-shaped context, general-knowledge
  traps, unsupported detail, authority conflict, and — at the provider level —
  citation of a non-retrieved chunk.
- CI gains a real quality signal that costs nothing and cannot leak a credential.

### Negative / accepted

- **Sixteen cases is a small sample.** One case is 6.25 percentage points of any
  whole-set rate.
- **The gates are uncalibrated.** They are engineering judgement, not derived
  from traffic or human labels.
- **The recall gate has little headroom** — 75% against an 81.8% measured
  ceiling — so a failure there should be investigated as a retrieval change
  before it is read as a generation problem.
- **The judge is correlated with the generator.** Same deployment, same weights,
  same failure modes; the bias runs towards leniency, which is the dangerous
  direction. LLM-judge scores are estimates, and human review remains necessary
  for consequential use.
- **Live evaluation is manual**, so a regression that only appears against a real
  model is found when someone runs it, not automatically.

## Related

- ADR 0006 — Microsoft Foundry capability lab (resource model, endpoints, RBAC).
- `products/platform-engineering-assistant/README.md` — commands and metric
  definitions.
