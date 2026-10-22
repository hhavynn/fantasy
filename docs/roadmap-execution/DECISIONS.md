# Roadmap execution decisions

Short, dated records. Each one gives the decision, why, and what would change it.

## D-01 · 2026-10-22 · One PR per issue, branches `feature/fi-<n>-<slug>`
Each issue is built in its own worktree from fresh `origin/main` and delivered as a focused PR. A dependent branch is
based on its prerequisite's exact commit and says so in the PR body. This checkpoint lives on
`claude/fantasy-intel-roadmap-b6nrc8`. Nothing is merged by agents.

## D-02 · 2026-10-22 · PRODUCT.md data-trust rules stay binding
No win probabilities, trade grades, acceptance likelihoods or expert-consensus numbers ship without a passing,
documented research gate (#174, #199, #185). #172 confidence must come from calibration evidence, not vote-style
claims. These rules change only through an explicit product-policy decision recorded here.

## D-03 · 2026-10-22 · Local Postgres for the security tier
The cloud container runs as root, and `initdb` refuses root. Start a disposable cluster as the `postgres` user on
127.0.0.1:55432 and export `FI_TEST_PG_DSN=postgresql://postgres@127.0.0.1:55432/postgres`. The harness's
safe-host check accepts it. CI keeps its own `postgres:17` service.

## D-04 · 2026-10-22 · Wave ordering versus the #205 phase list
#169 (rules), #168 (provenance contract) and #202 (QA gates) run first and in parallel because they touch disjoint
files. #201 (perf) moves to wave 2 because it shares `lib/api.ts` and backend caching with the provenance work.
#170 (shell redesign) runs after the other Phase 1 work and has sole ownership of `Nav.tsx`, `layout.tsx` and
`globals.css`.

## Open research gates (filled in as work reaches them)
- #186 news: needs a free source with redistribution rights; otherwise the profile ships without a news tab.
- #198 coverage/routes: no free licensed coverage data is known; expect a documented "derived stats only" scope.
- #173 live scoring: limited by provider polling cadence and quotas; no extra sync frequency without a budget.
