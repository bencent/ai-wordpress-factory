# AI WordPress Factory — Agent Instructions

## 1. Project Truth

This repository is the implementation authority for AI WordPress Factory.

- Git/repository state = project truth.
- pytest = behavior truth.
- Product contracts and accepted architecture decisions define intended behavior.
- Do not infer current implementation state from summaries when repository
  evidence is available.
- Inspect before changing.

## 2. Development Method

Use:

Product First
→ Vertical Slice
→ Contract Driven
→ Test Backed
→ Independent Review for critical changes

Prefer the smallest production slice that proves the approved behavior.

Do not expand an active slice merely because adjacent improvements are useful.

When a useful idea is outside the current contract:
record it as debt/backlog and keep the current implementation focused.

## 3. Scope Discipline

Before editing, identify:

- approved goal
- explicit invariants
- files likely involved
- out-of-scope areas

Do not silently redesign adjacent systems.

Do not combine unrelated refactors with feature work.

Do not implement future roadmap items inside the current slice.

## 4. Change Risk

Classify changes conceptually as:

### Fast
UI, CSS, copy, narrow presentation changes.

### Standard
API, services, workflow behavior, general application logic.

### Critical
Database migrations, persistence invariants, concurrency, fencing,
idempotency, security boundaries, recovery semantics, external side effects,
publication safety, or destructive operations.

Critical changes require:

implementation
→ focused tests
→ relevant regression tests
→ full regression
→ independent read-only review
→ commit

A passing test suite does not replace architecture/invariant review.

## 5. Git Safety

- Never commit unless explicitly requested.
- Never push unless explicitly requested.
- Never rewrite history unless explicitly requested.
- Do not discard or overwrite user changes.
- Inspect `git status --short` before editing.
- Keep unrelated changes out of the active slice.
- Run `git diff --check` before proposing a commit.
- Treat committed Git state as the authoritative project milestone.

## 6. Testing

For behavior changes:

- add focused deterministic tests
- run relevant regression suites
- run the full test suite before closing a critical slice

Do not weaken existing tests merely to make a new implementation pass.

When existing tests must change, classify the modification as:

- MECHANICAL_SCHEMA_UPDATE
- LEGITIMATE_BOUNDARY_GENERALIZATION
- TEST_WEAKENING
- UNRELATED_CHANGE

TEST_WEAKENING requires explicit justification and should normally block
acceptance unless the approved product contract intentionally changed the
protected invariant.

## 7. Review Findings

For critical independent reviews, a BLOCKER must include:

1. file / symbol / line range
2. current behavior
3. violated approved requirement
4. concrete reproduction
5. production consequence
6. minimum correction
7. required regression test

Do not classify speculative improvements as blockers.

Separate:

- concrete contract violations
- architecture debt
- hardening opportunities
- future product scope

## 8. Persistence and Concurrency

Prefer database-enforced invariants where practical.

Do not rely only on application-layer scoping when the database is expected
to protect an integrity boundary.

For worker/concurrency changes, consider:

- ownership
- leases
- fencing tokens
- stale workers
- idempotent replay
- transaction boundaries
- crash boundaries

Do not hold database transactions across external network calls.

Unknown external side-effect outcomes must fail safely rather than being
blindly retried.

## 9. Recovery Principles

Recovery != Retry.

Retry means re-executing work according to retry semantics.

Recovery means continuing from explicitly identified, durably persisted,
verified work.

Rules:

- Artifact != workflow_state.
- completed_stages is progress metadata, not reuse authority.
- Reuse requires explicit lineage.
- Reuse only durable, verified artifacts.
- Missing/invalid explicitly requested recovery artifacts fail closed.
- Source runs remain immutable history.
- Recovery creates/uses new execution history rather than resurrecting old
  failed runs.
- Do not silently regenerate when explicit recovery semantics require reuse.

Artifact provenance and provider execution policy are separate concerns.

Do not silently resolve historical-vs-current provider configuration policy.

## 10. Publication Safety

Content recovery and publication recovery are separate systems.

Publication uncertainty is handled by Reconciliation.

Never turn an unknown remote publication outcome into an automatic create
retry.

Do not infer publication success from Task state alone.

Preserve immutable publication authority and idempotency.

## 11. Secrets and External Systems

- Never commit secrets.
- Never print secrets into logs, tests, reports, or chat output.
- Store credential references, not credential values.
- Resolve secrets only at execution boundaries.
- Do not make external calls during read-only reviews.
- Do not perform live external writes without explicit authorization.

## 12. Architecture Boundaries

Keep these concepts distinct:

- Task = requested work
- TaskRun = execution attempt/history
- ContentVersion = immutable generated content
- Preview = persisted review representation
- Artifact = durable reusable intermediate work
- Approval = human authority
- PublicationRequest = durable publication intent
- Reconciliation = recovery from uncertain publication outcome

Do not collapse these into one state machine.

## 13. Current Recovery Direction

Phase 8.4 follows:

Reuse before Regenerate.

The first durable recovery artifact is PLAN.

A PlanArtifact is:

- DB-backed
- immutable
- bound to workspace/task/source run
- integrity verified
- reused only through explicit lineage

Do not generalize this into an all-stage artifact framework without an
approved contract.

## 14. Agent Roles

Implementation agents:
- implement only the approved slice
- provide evidence and tests
- do not self-expand scope

Independent reviewers:
- read only
- verify contracts and invariants
- distinguish blockers from debt
- do not modify code while reviewing

Product/architecture decisions that are unresolved must be surfaced rather
than silently decided in implementation.

## 15. Completion Discipline

Before declaring a slice complete, report:

- files changed
- behavior implemented
- migrations, if any
- focused test results
- regression results
- full-suite result for critical changes
- `git diff --stat`
- `git status --short`
- known deferred debt

Do not commit or push unless explicitly instructed.
