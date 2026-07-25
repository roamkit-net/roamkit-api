# AGENTS.md

Working rules for every AI agent and human contributor on RoamKit.
This file is about **how we work**, not product or stack specifics.
Architecture details live in ADRs under `roamkit-docs`.

## General principles

- Prefer small, reviewable pull requests.
- One architectural change per PR.
- Never mix schema changes, business logic, and frontend in the same PR.
- Every PR must pass CI before merge.
- Use Squash and Merge unless explicitly instructed otherwise.
- Keep `AGENTS.md` identical across all RoamKit repos (and the workspace root).

---

## Access and merge authority

RoamKit is a solo-operated org. Agents with write access must be able to finish the
delivery loop without waiting for a second human approver.

- Branch protection may require a pull request; it must **not** require approving
  reviews from another user (`required_approving_review_count = 0`).
- `enforce_admins` must stay **off** so maintainers/agents are not blocked.
- When the user asks to merge (or the agreed workflow reaches merge) and CI is green:
  squash-merge and delete the feature branch. Do not stall on “needs another reviewer”.
- Prefer HTTPS + `gh` / token for GitHub write operations in this workspace.
  SSH keys are optional and not required for PR/merge authority.
- Do not re-enable “required approving reviews ≥ 1” without an explicit human decision.

---

## Development workflow

Every feature follows this lifecycle:

1. Create feature branch from `develop` (not from a merged feature branch).
2. Implement only the agreed scope.
3. Run tests locally.
4. Commit logical units (Conventional Commits).
5. Push branch.
6. Wait for CI.
7. Fix CI failures.
8. Open Pull Request into `develop`.
9. Squash-merge when CI is green (and the user requested merge / delivery is complete).
10. Delete feature branch.

Never continue development on a merged branch.

**Branch model:** RoamKit app repos use **`develop` only** as the default and
integration branch. Do not open PRs against `main`, do not create `main`, and do
not assume a production `main` workflow exists unless the user explicitly starts
that phase. (A leftover `main` branch on GitHub, if present, is not part of the
active workflow.)

Staging deploys from `develop`.

---

## Pull Request rules

A PR should have exactly one responsibility.

Good examples:

- PR2 Billing schema
- PR3 CreditService
- PR4 Polygon provider

Bad examples:

- Billing schema + API + frontend
- Database changes + UI redesign

---

## Architecture

Architecture is governed by ADRs.

If an ADR is Accepted:

- do not redesign it
- do not introduce alternative implementations
- implementation must follow the ADR

Architectural discussions happen before implementation, not during it.

---

## Stop rule

If implementation requires changing an Accepted ADR:

**STOP.**

- Do not implement.
- Open an ADR discussion first.
- Never silently change architecture during implementation.

---

## Phase discipline

Implement only the current PR scope.

Do not work on future phases unless explicitly requested.

Future improvements belong in the plan, not in the current implementation.

---

## Database

- All migrations must be reversible when possible.
- Never modify production data outside migrations.
- Add tests for every migration affecting ownership or money.
- Prefer explicit CHECK constraints.

---

## Billing

Money is a critical domain.

Always preserve:

- Ledger is source of truth.
- `Account.balance` is cache.
- Ledger is append-only.
- Only `CreditService` may mutate balances.
- Never bypass `CreditService`.
- Never update ledger rows.

---

## Testing

Every PR must include appropriate tests.

Minimum:

- model tests
- service tests
- migration tests (when applicable)

CI must remain green. Fix CI on the same PR before merge.

---

## Git

Commit messages follow Conventional Commits.

Examples:

```text
feat(billing): add Account model
fix(api): prevent duplicate deposits
test(billing): add ledger tests
docs(adr): update ADR-010
docs(agents): clarify solo merge authority
```

---

## Scope control

Do not add extra features.

If a requested change belongs to another future PR:

- document it
- do not implement it

Avoid scope creep.

---

## Code quality

Prefer:

- simple
- explicit
- readable
- deterministic

Avoid:

- premature abstraction
- unnecessary generic frameworks
- hidden side effects

---

## Communication

When proposing changes:

- explain why
- explain impact
- explain migration risk

If the design is locked, implementation should follow it rather than reopen discussion.
