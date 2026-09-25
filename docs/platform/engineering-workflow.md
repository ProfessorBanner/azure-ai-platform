# Engineering Workflow

The canonical interaction standard for this repository: how work is scoped,
implemented, reviewed, debugged and proven, by human engineers and AI coding
agents alike. `CLAUDE.md` states the platform rules; this document states the
working method. Platform facts live in `docs/platform/current-state.md` and the
ADRs and are not repeated here.

## 1. Operating modes

Every task runs in exactly one mode. Name it in the task.

**BUILD** — deliberate implementation work. Architecture and change proposals
are allowed, but only within the explicit task scope. Work outside that scope is
raised, not performed.

**DEBUG** — hypothesis-driven troubleshooting. Isolate the failed layer, preserve
already-proven systems, run the smallest discriminating test first. No
opportunistic refactoring: a fix is the minimum change that resolves the proven
cause.

**REVIEW** — inspect an existing change or output. Report critical and material
problems only. Do not introduce new work unless it is required for correctness
or safety.

## 2. Execution contract

Every implementation task establishes these six things up front. If any is
missing, agree it before writing code.

- **Objective** — the single outcome the task delivers, in one sentence.
- **Invariants** — what must remain true and untouched (deployed state, resource
  addresses, identities, public interfaces, promotion semantics).
- **Acceptance evidence** — the concrete artefact that proves the objective
  (a command's output, a diff, a plan, an API response), not an assertion.
- **Required proof level** — the L0–L5 level (§3) at which that evidence is
  sufficient.
- **Allowed change scope** — the explicit files or systems that may change.
  Everything else is out of scope.
- **Stop conditions** — the point at which the task is complete and work halts
  (§7).

## 3. Proof levels

- **L0 — Static.** Syntax, formatting, schema checks, unit tests. No credentials,
  no remote state.
- **L1 — Resolution.** Resolved configuration: target values, substituted
  variables, IDs, generated configuration.
- **L2 — Authorization / API.** Identity, RBAC, Unity Catalog grants, workspace
  assignments, API-visible state.
- **L3 — Plan.** Terraform plan, Bundle plan, or equivalent dry-run / change
  preview.
- **L4 — Deployment.** Resource or configuration successfully deployed and
  visible in the target system.
- **L5 — Runtime.** Actual workload execution using compute and runtime
  semantics.

**Governing rule: never use a higher proof level when a lower level is
sufficient to prove the acceptance criterion.**

In particular: **never start compute merely to prove configuration that can be
established at L0–L4.** Compute is proof of runtime behaviour, not of
configuration correctness — and it costs money and time.

Proof levels are not a ladder to climb. A task requiring L3 is finished at L3.

## 4. Change-agent contract

For scoped implementation work, the coding agent:

- reads the existing implementation before changing it;
- changes only the explicitly allowed files or systems;
- preserves the stated invariants;
- runs the cheapest sufficient checks for the required proof level;
- shows the resulting `git diff`;
- stops at the defined stop condition.

Unless the task explicitly requests it, the agent must **not**:

- deploy or apply infrastructure;
- run Databricks compute or jobs;
- commit;
- push;
- broaden scope;
- refactor unrelated code.

This contract is subordinate to the approval list in `CLAUDE.md`: commands named
there require explicit human approval even when a task appears to authorise them.

## 5. Review hierarchy

Evaluate implementation evidence in approximately this order, strongest first:

1. Runtime evidence (the workload actually ran and produced the expected result)
2. CI / integration result
3. Plan or deployment evidence
4. Tests and static checks
5. `git diff`
6. Agent explanation

**Review begins with the actual changed files and the `git diff`, not the agent
narrative.** The narrative is the weakest evidence in the list and is read last,
to check it against what the diff shows — never in place of it. An explanation
that disagrees with the diff is wrong by definition.

Note the asymmetry with §3: the *cheapest sufficient* level is what a task must
produce, but when evidence exists, the *strongest* available is what a review
weighs.

## 6. Debugging protocol

Structure every debugging step as:

```
FAILED LAYER              which layer actually failed, and how that is known
ALREADY PROVEN            what is established and must not be re-litigated
PRIMARY HYPOTHESIS        one specific, falsifiable cause
CHEAPEST DISCRIMINATING   the smallest test that distinguishes it from the
TEST                      alternatives, and at the lowest proof level that can
DO NOT CHANGE             the proven systems and invariants that stay untouched
NEXT                      the single next action, conditional on the test result
```

Hold **one** primary hypothesis and **one** discriminating test at a time.
Branch only when the evidence genuinely requires it — parallel speculation
destroys the signal that makes the next test informative.

## 7. Stop conditions

Every task has an explicit stop condition. **Stopping once the evidence is
sufficient is part of correctness**, not a lack of thoroughness. Continuing past
it spends money, adds risk and produces changes no one reviewed.

Examples:

- **Bundle configuration task** — validate / resolve / plan succeeds → stop.
  Do not run the workload.
- **Authorization task** — API or grant state proves the required access → stop.
  Do not modify networking.
- **Runtime task** — one deliberate successful workload execution proves the
  integration → stop. A second run proves nothing new.
- **Documentation task** — the file exists and the diff is reviewed → stop.

If the stop condition is reached but part of the scope is blocked, say so
explicitly rather than substituting adjacent work.

## 8. CI philosophy

Local deterministic tests should catch deterministic errors before PR CI
wherever practical: formatting, linting, typing, unit tests, schema validation
and configuration resolution are cheap locally and slow in a pipeline.

CI should increasingly act as **confirmation and integration proof** rather than
as the primary debugger. Using CI to iterate on a deterministic error is a slow,
expensive local test.

This is not a claim that every Azure DevOps behaviour is reproducible locally.
Pipeline-resource variables, triggers, Environment approvals, service-connection
OIDC and agent-image behaviour are genuinely pipeline-only, and proving them
requires a run. Reproduce locally what is deterministic; use CI for what is not.

## 9. Session / bootstrap order

For a fresh engineering session, read in this order:

1. `CLAUDE.md` — platform rules and constraints
2. `docs/platform/current-state.md` — what exists now
3. `docs/platform/engineering-workflow.md` — this document
4. The relevant ADR(s) under `docs/adr/`
5. The relevant source files

Do not reconstruct platform state from conversation history. If this document
and the repository disagree, the repository wins and this document is stale —
fix it.
