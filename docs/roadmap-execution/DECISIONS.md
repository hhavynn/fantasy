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
reached `docs/agent-workflow.md` (inherited by new worktrees) with PR #237 (e6bb3f3).

## D-10 · 2026-10-23 · Codex review quota exhausted; one independent Claude review replaces it
On PR #241 Codex reported that the account reached its code-review usage limit. Buying credits is outside the
zero-spend rule, so Codex review is unavailable until the quota resets. Until then, each PR gets one independent,
read-only review before merge. Security-sensitive diffs (auth, RLS, /v2, tenant isolation, cross-league data) get the
`multileague-security-reviewer` subagent. Other diffs get one correctness review from a subagent given the diff, not
the author's conclusions. This replaces the lost reviewer; it is not an extra round. Merge gates are otherwise
unchanged (D-07).

## D-11 · 2026-10-23 · #186 news gate: no news-headline feed; "Availability events" instead
The licensing memo (PR #247, `docs/research/186-player-news-sources.md`) found no free source that permits
automated fetch, cache and display of third-party news headlines. NFL.com, team sites, ESPN endpoints and Google
News bar it. Sleeper, Reddit and the publisher RSS feeds are unclear or need consent. So no news tab or headline
feed ships.
What may ship instead is a separately labelled "Availability events" list:
- Content: app-written, templated events from nflverse injuries and weekly rosters (CC BY 4.0), with no provider
  free text and no cause, timeline or "confirmed" claims.
- Attribution: "nflverse (CC BY 4.0), reformatted".
- No new fetches. Roster filtering happens server-side behind `authorize_league`.
Caveats: the memo's non-nflverse verdicts rest on search excerpts (the sandbox could only reach GitHub), and the
nflverse upstream provider is unverified. Changes if: a source's full terms explicitly grant display in third-party
apps, or the owner obtains written consent (e.g. from Sleeper).

## D-12 · 2026-10-23 · League Compatibility & Scoring Correctness workstream (#253, #254)
Owner request: track #253 (P0) and #254 (P1) as a separate workstream. The combined scope is 62 issues. #205 (40) and
#227 (18) keep their original scope and numbering.
- **Ownership:** #253 owns end-to-end compatibility fixtures, the per-tool gap inventory and the diagnostic contract.
  #254 owns scoring-fidelity changes and consumes the contract. Neither duplicates the other's logic.
- **Diagnostic contract:** reuse the `handled / partial / unsupported / unverified` vocabulary already in
  `analytics/league_rules.py` (#169). "Ready" in the issue text means `handled`. Each entry carries a reason, the
  missing input, the provider boundary and an action. It reports four separate dimensions: import readiness, roster
  legality, scoring/projection fidelity, and feature availability.
- **Roster support is not scoring fidelity.** A structurally supported league can still get partial or unsupported
  scoring for some tools, and the diagnostics say so per tool.
- **P0 meaning:** unsupported behavior is reliably identified and disclosed. It is not a promise that every rule will
  be implemented. Abstention and research-only outcomes are valid.
- **Ordering:** #253 starts after #249 merges, audit-first, in one small backend PR. #254 implementation follows the
  contract and the confirmed gaps. The workstream takes the priority slot; other roadmap work continues in the second
  slot when file ownership is disjoint. Nothing else is paused or deferred wholesale.
- **Fixtures:** synthetic only. FX-17 reproduces the structure of a real private six-team ESPN league (2 QB, 3 RB,
  4 WR, 2 TE, 1 OP, 3 FLEX, K, D/ST, 10 BE, 3 IR). Its real scoring modifiers are unverified, and its name, ids,
  members and rosters never appear in the repository.
- **Product claims:** no claim of universal support. ESPN/Sleeper capability is verified per integration and
  deployment. Yahoo stays unavailable. Unsupported IDP slots, scoring overrides and missing data are disclosed. Missing
  is never zero. Independent scoring never poses as league-specific. No paid APIs, provider writes or scraping.

## D-13 · 2026-10-23 · Continuous lane pipeline (throughput without weaker gates)
Bottlenecks observed: the coordinator waited serially on CI, on reviews and on merges, with independent issues idle
and up to one implementation agent active. It also posted repeated status messages about unchanged runs.
- **Lanes.** Up to three delegated agents at once, when independent. A = integration and unblocking (usually the
  coordinator itself: review fixes, conflicts, merges). B = the highest-value ready feature. C = a second independent
  feature, a bounded fix or a read-only audit. Lanes are logical; capacity is reassigned as work finishes.
- **Selection (STATUS "Work queue").** Order by priority (P0 > P1 > P2, adjusted for release impact), then:
  dependencies merged, existing engines reusable, no file overlap with an active owner, research and licensing gates
  clear, and review risk. When an agent finishes, the next ready item starts without asking the owner, within the
  existing roadmap authorization. Provider integrations and writes, paid services, secrets and infrastructure still
  need explicit authorization.
- **CI and review are non-blocking.** Push, then keep working. Don't poll an unchanged run. Never re-run a SHA
  without a diagnosed reason. Don't push a minor change that cancels a useful run. On a failure, read the failing job
  first. Cancelled ≠ passed. Defer only the work that a pending merge would invalidate, and name that dependency.
- **Stalls.** After about 15–20 min with no observable progress, inspect the real state (agent output, worktree
  diff, CI) before calling it stalled. If blocked: name the blocker, keep other lanes moving, and fix simple
  conflicts or tests in the owner's worktree. If an agent has stopped, take over from its handoff without losing work.
  Escalate to the owner after about 30 min of verified inactivity or failed recovery.
- **Reporting.** Report milestones only: a PR opens, a material review finding, a real CI blocker, a resolved
  dependency, a merge, a stuck agent, or a queue change. STATUS updates stay compact; no summarizer agents.
- **Shared resources.**
  - One heavyweight local slot (D-08) and one Playwright slot (fixed ports), each assigned per agent in its brief.
  - The disposable Postgres cluster (D-03, /tmp/fitestpg) is coordinator-owned; agents never stop or wipe it.
- **Unchanged:** D-07 exact-head CI and merge rules, D-08, D-09, D-10 independent review, tenant isolation, the
  credential scan, check integrity, provenance and missing ≠ zero, provider read-only terms.

## D-14 · 2026-10-23 · Merge-first scheduling, CI as the heavy gate, overlapping reviews
Bottleneck: finished work piled up behind one local heavyweight slot and behind serial review rounds. One PR (#258)
also sat in conflict with main without CI ever running.
- **WIP limit (temporary):** at most two feature PRs in implementation or review fixes at once. Narrow, independent
  correctness fixes (e.g. G5, #254 phase 1) may run alongside. No new feature issues just to fill lanes. The third
  feature lane comes back once the queue is below about 3 open PRs and merging steadily.
- **Heavy local tiers:** exact-head CI already runs the full backend suite, the security tests and every E2E suite in
  parallel runners. Local `affected`/`security`/Playwright are no longer required before opening a PR. Agents run
  targeted tests plus `quick`, and Playwright only for a spec they are writing. The single local heavyweight slot stays
  (4 CPUs and 15 GB; an earlier batch was OOM-killed with exit 137). pg_harness already isolates databases per session.
  The slot is used only when CI cannot reproduce a problem.
- **Overlapping reviews:** a clear Codex or reviewer finding goes to the PR owner at once. Other reviews continue, later
  findings are folded in, and one consolidated push follows. Never merge with a material finding open. Re-run CI on the
  new exact head, and re-review the changed areas.
- **One candidate per round:** batch fixes into one push. Don't push trivial changes that restart CI. A critical
  security fix is never held back for batching.
- **Conflicts block CI:** GitHub runs no `pull_request` CI on a PR that conflicts with main. Check `mergeable_state`
  whenever main moves, and merge main into open PRs (merge commit, never a force-push).
- **Pre-PR checklist** (from findings on #249, #255, #256, #257, #258; one line each in the PR body):
  1. Missing input never becomes 0 (totals, values, gains, mixes, replacement levels).
  2. A source with no non-null values is never named as the source ("Unavailable" instead).
  3. Per-provider assumptions are stated or gated (Sleeper waiver status, IR rules, IDP rows).
  4. Combined effects are evaluated, not item by item (multi-claim spots and limits, multi-slot totals).
  5. Cache keys include every input that changes output (repeated params in order, mode flags).
  6. Fetches, forms and links stay league-scoped (`/l/<lg>/`, `/v2`), never relying on the last-league cookie.
- Unchanged: D-07 exact-head CI, D-09, D-10 independent review, tenant isolation, credential scan, missing ≠ zero,
  provenance, provider read-only terms.

## D-15 · 2026-10-23 · Codex is the default independent reviewer again; Claude is the fallback
Owner decision; supersedes the reviewer choice in D-10 (Codex quota has returned). Everything else in D-10/D-14 stands.
- **Default:** when an implementation PR is ready, the independent code review is Codex's diff-scoped review (it runs
  on PR open; `@codex review` requests it for an updated head, `@codex security review` for security-sensitive diffs).
- **No duplicates:** no Claude review agent runs in parallel with a Codex review or re-checks work Codex and CI already
  verified. A pending Codex review is not a reason to start a Claude one.
- **Fallback:** only when Codex reports a usage/quota limit or is genuinely unavailable, a separate Claude agent that
  did not author the change does the review (the `multileague-security-reviewer` for security-sensitive diffs).
- **After fixes:** request a fresh Codex review of the updated diff when the fix changes reviewed logic.
- **Unchanged gates:** mandatory independent security review for security-sensitive diffs, exact-head CI, no open
  material findings, no conflicts. Claude's role: implementation, integration, coordination, fixing findings.
- Transition: Claude reviews already running when this was decided (#283, #284) finish; none are restarted.

## D-16 · 2026-10-23 · Merge-first execution; owner decisions on #277 budget and #147
- Merge-first mode: each PR merges the moment its own gates pass (exact-head CI, Codex/independent review with no material
  findings, no conflicts); one cohesive fix round per review; targeted local tests, CI as the full gate; no new large
  features until the integration queue shrinks.
- #277 beam work budget default stays 15,000 per opponent (owner decision; revisit only on evidence it is unsafe or
  impractical). The exact search (stages A–C) is never budgeted; truncation is disclosed.
- #147 (Shorts paper style) stays open, parked outside the roadmap integration queue.
- Follow-up 1t approved: sign-out clears only app-owned account/league browser storage; test account switching.

## Open research gates (filled in as work reaches them)
- #186 news: resolved by D-11 (no headline feed; Availability events from nflverse data allowed).
- #198 coverage/routes: no free licensed coverage data is known; expect a documented "derived stats only" scope.
- #173 live scoring: limited by provider polling cadence and quotas; no extra sync frequency without a budget.
