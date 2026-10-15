# Waiver Intelligence V3 Implementation Plan

> Execute inline using superpowers:executing-plans; the user's supplied brief is the approved implementation scope.

**Goal:** Explain which available players improve my team, the legal roster move, its sacrifice, and waiver resource cost.

**Architecture:** Pure waiver-only decision modules consume a batched adapter over stored data. Existing recommendations gain versioned diagnostics in components JSON. A compact filtered decision list replaces the large table.

**Tech stack:** Python/FastAPI/DuckDB, Next.js Server Components, existing CSS.

**Spec:** User's Fantasy Intelligence V3 request; confirmed audit in fantasy-intel/docs/waiver-v3-audit.md.

## Constraints

Independent worktree/branch from current origin/main. No merge. No protected trade files, scoring changes, shared injury changes, migrations or render-time network requests. Preserve compatibility fields where practical; explicitly label unknown settings and PPR heuristics.

## Review focus

IR activation and stale snapshots must not imply open capacity. An add/drop must respect all known caps and preserve flex coverage. Unknown drop value cannot become zero. A high-priority manager must be able to prefer a comparable free agent. Missing projection/schedule and a finished scoring window must suppress confident streaming advice.

## Tasks

- [x] 1. Add failing legality/drop tests in backend/tests/test_waiver_decisions.py; implement waiver_roster.py with roster_constraints and plan_roster_move. Evaluate all possible single drops and explain exclusions/sacrifices; compare current and forward lineup effects.
- [x] 2. Add failing acquisition/resource/streaming tests; implement waiver_decisions.py with acquisition_utility, resource_diagnostics, evaluate_streaming and classify_action. Keep benefit, cost and quality separate.
- [x] 3. Add fixture pipeline tests; implement waiver_data.py to batch stored projections, schedules, settings and candidates. Replace global candidate cap with bounded per-position evidence-aware selection and expose exclusions. Integrate V3 diagnostics in waivers.py and sort/filter only the waiver endpoint.
- [x] 4. Update /waivers with local types/styles, decision views, roster impact, evidence and risks; retain player/manager drill-downs. Use existing PRODUCT.md and app conventions.
- [x] 5. Run full pytest, frontend lint/build and benchmark script for realistic 10/12/14-team pools. Document before/after scenarios, constraints, performance and integration needs.
- [ ] 6. Review diff against protected paths; commit, push, create/attach focused PR, request Codex review, address actionable findings and report exact results.

## Decisions and verification ledger

- Audit completed before implementation. Existing user checkout has unrelated edits; isolated managed worktree created from fetched origin/main.
- Follow the user's explicit end-to-end implementation/delivery authorization without extra design approval checkpoints. No subagents needed for implementation.
- RTK binary is absent; use underlying tools and report that limitation.

- Implementation complete: legality/drop, utility/resources, streaming, batched adapter, coverage and compact decision UI.
- Verified RED→GREEN on IR eligibility, unknown player value, exhausted FAAB, low-owned coverage, stale roster/projection data, alternatives, streaming replacements and future positional holes.
- Independent review findings reproduced and resolved. Full suite: 494 passed, 1 skipped, three confirmed baseline projection-backtest failures; focused waiver/API: 69 passed. Frontend lint/build passed.
- Benchmark final synthetic 10/12/14-team builds: 6.390/7.277/6.173s; stored-results API 108.79/79.14/79.02ms.
- Ruling: preserve known failing backtest module unchanged and disclose baseline failures in the requested PR, rather than expanding concurrent-work scope.
