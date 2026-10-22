# Roadmap execution decisions

Short, dated records. Each one gives the decision, why, and what would change it.

## D-01 · 2026-10-22 · One PR per issue, branches `feature/fi-<n>-<slug>`
Each issue is built in its own worktree from fresh `origin/main` and delivered as a focused PR. A dependent branch is
based on its prerequisite's exact commit and says so in the PR body. This checkpoint lives on
`claude/fantasy-intel-roadmap-b6nrc8`. Implementation agents never merge (see D-07).

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

## D-05 · 2026-10-22 · #228 before #229, then the expansion
#228 (taxi) changes `sleeper/write.py`, `trade_data.py` and the lineup consumers. #229 changes the explanatory
baselines in `trade_assets.py` and leaves the exact evaluator alone. Their files don't overlap, but both shape what
#182/#184/#216 build on, so #229 takes the next free agent slot and starts from #228's branch if #228 is still
unmerged. Both come before any expansion feature that reads roster capacity or replacement levels (#216, #218, #214).

## D-06 · 2026-10-22 · How the #227 expansion is scheduled
It is an additive backlog with its own STATUS table. An expansion issue starts only when a slot frees, its
prerequisite has landed (or can be safely stacked on), and no active agent owns the same files. When an expansion
issue is ready, its backend research (#212, #210, #226, #225) may run before its UI; the UI waits for #170, the
owner of nav and layout. Prefer a section or tab of an existing page over a new page (Game Splits in Research,
attribution in Projection movers). #224, #220 and #223 are research-first: the deliverable is a go/no-go note.

## D-07 · 2026-10-22 · Owner authorization to merge roadmap PRs; integration queue
The owner authorized the coordinator to merge #205, #227 and related fix/security PRs, one at a time, by squash. A PR
merges only when CI is green on its exact head SHA, it has no unresolved material review or security findings, it has
no conflicts, and no protection is bypassed. There are no admin overrides, force pushes or deploys. Implementation
agents never merge.
- Target at most 2 verified-but-unmerged feature PRs; if the queue grows, the coordinator integrates before starting
  new work.
- When a PR is stale but compatible, it merges as is. When a merge with main touches files shared with the merged
  work, the affected tests run locally and CI must pass on the new head first. Full local suites are not rerun for code
  already verified by CI.
- A branch stacked on a squash-merged parent is retargeted to main and merged with main, keeping its own side of
  files the parent already contained, after checking that main matches the parent's final commit there.

## D-08 · 2026-10-22 · Verification budget and one heavyweight local slot
An audit of #207–#235 found that agents follow the workflow ladder (quick/affected/security). The waste came from
the coordinator: after `affected` missed the app-wide source audits (fixed in PR #236), it told every agent to run
the full backend suite locally. Four to six of those ran at once, which caused the Postgres outage, the full disk and
quick-tier timeouts. Rules from now on:
- Agents run targeted tests, `quick`, `affected` (which now includes the app-wide audits) and `security` when
  required. They do not run full backend suites or local Playwright suites; the PR's CI on the exact SHA is the full
  gate.
- Only the coordinator runs heavyweight local checks, one at a time (a full shard set or a Playwright suite), and only
  when CI can't answer the question first (e.g. a merge combining overlapping UI work).
- An environment failure (connection refused, ENOSPC, initdb) is diagnosed once from the compact log before any
  rerun, and never reported as a code defect.
- Bounded mechanical tasks (e.g. fixing a well-specified review finding in a new session) may use a cheaper model.
  Architecture, security and correctness work keeps the strongest model. Running agents are not interrupted to
  switch.

## D-09 · 2026-10-22 · Shared cleanup belongs to the coordinator
An implementation agent deletes only what it created. Shared scratchpads, test databases or clusters, artifacts,
logs, caches and other agents' worktrees are cleaned up by the coordinator after it checks for active users. Wildcard
`rm` over a shared directory is not allowed. Why: the #229 agent ran `rm -rf <session scratchpad>/*`. D-08 and D-09
reach `docs/agent-workflow.md` (inherited by new worktrees) through PR #237. Until #237 merges, agent briefs repeat
them.

## Open research gates (filled in as work reaches them)
- #186 news: needs a free source with redistribution rights; otherwise the profile ships without a news tab.
- #198 coverage/routes: no free licensed coverage data is known; expect a documented "derived stats only" scope.
- #173 live scoring: limited by provider polling cadence and quotas; no extra sync frequency without a budget.
