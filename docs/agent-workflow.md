# Fantasy-Intel Verification Workflow

These are the authoritative shared project rules for Codex, Claude Code, and Gemini/Antigravity. Read this file when entering the repository; the entrypoint files and native skill are adapters, not separate policies.

Apply these rules whenever verifying changes, running tests, or validating multi-league features in `hhavynn/fantasy`.

## Verification Philosophy

Prevent unnecessary token waste, context pollution, and redundant test executions:
- **Never run full test suites during inner-loop TDD.** (The full backend suite takes ~9-19 minutes locally).
- **Run the smallest sufficient test set** matching your changes.
- **Escalate verification tiers** as work matures from idea to implementation to review to PR gate.
- **Keep tool outputs compact.** Let `agent_check.py` format results; never dump 1,000 lines of test logs into context.

---

## The Verification Ladder

```
[Level 1: Inner-Loop TDD]
  ↳ Failing test → Implementation → Directly affected test (1-3s)
  ↳ Command: `python fantasy-intel/scripts/agent_check.py quick`

[Level 2: Pre-Commit / Pre-Review]
  ↳ Static checks + all affected backend/frontend suites
  ↳ Command: `python fantasy-intel/scripts/agent_check.py affected [--base origin/main]`

[Level 3: Security & Multi-League Audit]
  ↳ If touching RLS, migrations, auth, crypto, worker fencing, or /v2 routes:
  ↳ Command: `python fantasy-intel/scripts/agent_check.py security`
  ↳ Apply `docs/agent-security-review.md` read-only before external review. Claude may dispatch its native reviewer; other agents may review directly.

[Level 4: Final PR / Phase Acceptance Gate]
  ↳ Full backend, frontend (lint/typecheck/build/unit), golden checks, and E2E
  ↳ Command: `python fantasy-intel/scripts/agent_check.py full`
  ↳ Verified against the exact final commit SHA
```

---

## Fast Verification Commands

| Goal | Command | Duration |
|------|---------|----------|
| Check local environment health | `python fantasy-intel/scripts/agent_check.py doctor` | <1s |
| Clean orphaned test postgres clusters | `python fantasy-intel/scripts/agent_check.py doctor --clean-orphans` | <1s |
| Fast AST static security checks | `python fantasy-intel/scripts/static_checks.py` | <0.1s |
| Fast TDD verification | `python fantasy-intel/scripts/agent_check.py quick` | ~3s |
| Conservative affected tests | `python fantasy-intel/scripts/agent_check.py affected` | ~3-10s |
| Security regression suite | `python fantasy-intel/scripts/agent_check.py security` | ~15s |
| Full verification gate | `python fantasy-intel/scripts/agent_check.py full` | CI / final gate |

---

## Conservative Test Mapping Policy

When running manual tests or interpreting `agent_check.py`:

- **SQL Migrations or RLS (`supabase/migrations/*.sql`)**:
  Must run: `test_pg_harness.py`, `test_control_plane_pg.py`, `test_control_plane_rls.py`, `test_control_plane_mutations.py`, `test_control_plane_functions.py`, `test_sync_queue.py`, `test_credential_crypto.py`.
- **FastAPI Auth & Route Changes (`app/control_plane/`, `app/api/`)**:
  Must run: `test_jwt_auth.py`, `test_authorize_league.py`, `test_v2_authorization_matrix.py`, `test_league_scope_audit.py`.
- **Sync Workers (`app/sync/`)**:
  Must run: `test_sync_worker.py`, `test_sync_queue.py`, `test_sync_publish.py`, `test_credential_crypto.py`.
- **Frontend Changes (`fantasy-intel/frontend/`)**:
  Must run: `npm run lint`, `npm run typecheck`, and `npm test`.
  If touching `app/` (routes, server actions) or config files: also run `npm run build`.
- **Any backend `app/` source (including a new module)**:
  Also run the app-wide source audits (`test_weekly_learning.py`, `test_dst_research.py`, `test_independent_projections.py`); `agent_check.py affected` adds them automatically.
- **Analytics & Projections (`app/analytics/`)**:
  Must run matching test suite (`test_trades.py`, `test_start_sit.py`, etc.) + `test_league_scope_audit.py` + `test_golden_legacy.py`.

---

## Diagnosing Failures Without Context Pollution

1. Run verification through `agent_check.py`.
2. Inspect the compact summary table.
3. If a test fails, `agent_check.py` writes full stdout/stderr to `.agent_diagnostics/<run_id>_<step>.log`.
4. Use `read_file` with targeted line ranges to inspect the specific failure lines in the log file, instead of re-running the entire suite with verbose flags.
5. If database tests hang or report connection errors, run `python fantasy-intel/scripts/agent_check.py doctor --clean-orphans`.

---

## Supporting Architecture & References

For subsystem directory layout, authoritative design documents, and known dangerous architectural boundaries, consult:
[architecture-map.md](../.claude/skills/fantasy-verify/references/architecture-map.md)

## Exploration, safety, and isolation

- Use Graphify query-first when available (`./scripts/graphify-run query`, `explain`, `path`); otherwise use targeted `rg` or symbol lookups.
- Consult the architecture map only for the subsystem being changed; do not automatically load the master plans.
- Inspect existing worktrees and concurrent changes before editing. Use an isolated feature branch/worktree, preferably `.worktrees/<branch-name>` or a harness-managed worktree. Never reset or overwrite unrelated changes.
- Never mutate production databases, cloud services, live credentials, or external provider sessions while developing or reviewing. Tests must use local synthetic fixtures.
- Do not add hooks that silently run expensive suites. Doctor is diagnostic; use `--clean-orphans` only after inspecting its safe ownership classification.

## Security review and PR acceptance

- For changes to auth, RLS, migrations, credentials, worker fencing, `/v2` routes, or tenant isolation, run the security tier and apply the same diff-scoped [security checklist](agent-security-review.md). Native Claude subagents are optional integrations; the checklist is required regardless of agent.
- Client-side league isolation also merits a diff-scoped checklist review, including storage keys, scope transitions, and unscoped routes. State explicitly whether an automated security suite ran or only a manual review.
- Preserve 404 indistinguishability for inaccessible private leagues; supplied invalid JWTs return 401. Preserve RLS, transaction-local claims, worker fences, and secret redaction. See the checklist for details.
- Before PR review run `affected --base origin/main`, required frontend checks, and feature-specific tests. Run `full` once at the final gate, not on every edit.
- Record the exact final commit SHA, commands, durations, selected tests, and outcomes. Any missing gate is UNVERIFIED until GitHub Actions confirms it on that exact SHA; narrow tests do not substitute for full CI. Never claim skipped, dry-run, or manual checks as automated passes.
- Open focused PRs; do not automatically merge.
