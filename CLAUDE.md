# Claude Code Assistant Guide - Fantasy-Intel

This repository is `hhavynn/fantasy`, a high-performance fantasy football analytics and multi-league platform.

## Key Principles & Token Efficiency

1. **Never run full test suites during inner-loop TDD.**
   - The full backend suite takes ~9-19 minutes locally.
   - Use `python fantasy-intel/scripts/agent_check.py quick` (1-3s) or `affected` (3-10s).
   - Use `full` only at the final phase/PR gate.
2. **Compact diagnostics over context dumping.**
   - `agent_check.py` formats concise PASS/FAIL tables and saves verbose output to `.agent_diagnostics/`.
   - Never run unconstrained verbose tests that print thousands of log lines into context.
3. **Targeted exploration.**
   - Use Graphify query-first when available (`./scripts/graphify-run query`, `explain`, `path`).
   - If unavailable, use targeted `rg`/`git grep` and symbol lookups; avoid reading large directories whole.
   - Reference `architecture-map.md` in `.claude/skills/fantasy-verify/references/architecture-map.md`.
4. **Worktree & safety isolation.**
   - Never wipe or reset someone else's uncommitted working tree changes.
   - Use clean worktrees in `.worktrees/<branch-name>` for distinct feature or phase work.
   - Run `python fantasy-intel/scripts/agent_check.py doctor` to diagnose environment and socket issues.

## Verification Tiers

| Tier | When to Use | Command |
|------|-------------|---------|
| **Doctor** | Diagnosing local test environment / hung DBs | `python fantasy-intel/scripts/agent_check.py doctor [--clean-orphans]` |
| **Quick** | Inner-loop TDD (test → code → verify) | `python fantasy-intel/scripts/agent_check.py quick` |
| **Affected** | Before committing or requesting review | `python fantasy-intel/scripts/agent_check.py affected [--base origin/main]` |
| **Security** | Auditing auth, RLS, migrations, crypto, sync | `python fantasy-intel/scripts/agent_check.py security` |
| **Full** | Final PR acceptance criterion | `python fantasy-intel/scripts/agent_check.py full` |

## Security & Subagents

- For multi-league security or tenant isolation changes, invoke the subagent `.claude/agents/multileague-security-reviewer.md` before external Codex review.
- The reviewer audits 401 vs 404 behavior, RLS policies, connection-pool claim isolation, worker fencing, and secret redaction.

## Development Reference & Subsystems

- Architecture map & dangerous boundaries: `.claude/skills/fantasy-verify/references/architecture-map.md`
- Master multi-league plan: `docs/superpowers/plans/2026-10-19-multi-league-platform.md`
- Multi-league design spec: `docs/superpowers/specs/2026-10-19-multi-league-platform-design.md`
