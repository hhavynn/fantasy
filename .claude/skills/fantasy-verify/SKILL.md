---
name: fantasy-verify
description: Repository-specific verification workflow for fantasy-intel. Guides fast test selection, tiered verification, security reviews, and phase gate checks.
---

# Fantasy-Intel Verification Workflow

Use this skill whenever verifying changes, running tests, or validating multi-league features in `hhavynn/fantasy`.

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
  ↳ Dispatch `.claude/agents/multileague-security-reviewer.md` before external review

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
[architecture-map.md](references/architecture-map.md)
