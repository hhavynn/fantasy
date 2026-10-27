# Guided onboarding acceptance (#167)

Scope: the existing wizard from #233/#316/#319, with #333's first-sync guidance merged in #355.
This evidence uses invented leagues, accounts and credentials only. No production account or provider session is used.

## Routes and launch conditions

The supported hosted journey has `MULTILEAGUE_UI_ENABLED=1`, `MULTILEAGUE_ENABLED=1`,
`MULTILEAGUE_CONNECT_ENABLED=1`, and `CLASSIC_SITE_ENABLED=0`. Account registration retains its separate gates.
A fresh guest at `/` is redirected to `/leagues` by `proxyDecision`; the public demo is discoverable there before sign-in.
The historical question about adding a demo link to the classic Overview no longer blocks that hosted journey.
The classic site and deployments with multi-league UI disabled retain their existing behavior; the demo remains flag-gated.

| Journey | Routes | Access and completion |
|---|---|---|
| Guest/demo | `/` → `/leagues` → `/leagues/demo` | No account; explicit synthetic label; connect CTA returns to `/leagues` |
| Public ESPN | `/leagues?host=espn` → `?league=<id>` → `/l/<id>` | Sign-in to import; no provider cookies; ready CTA requires an authorized snapshot |
| Private ESPN | `?host=espn` → `?host=espn&private=<external-id>&season=2026` → `?league=<id>` | Own cookies, explanation and required consent; rejected access offers invite/retry guidance |
| Sleeper | `/leagues?host=sleeper` → `?league=<id>` → `/l/<id>` | Sign-in to add; public provider read; no cookie form |
| Existing manager | `?league=<id>&already=1` | Reuse existing league; progress until ready; dashboard stays scoped to that league |

## Browser evidence matrix

`frontend/e2e/onboarding-multileague.spec.ts` adds three journeys at **320, 390, 768 and 1440 px**.
Each quality checkpoint checks structure, broken/missing values, app-bar fit, overflow and axe in **light and dark**.
Each journey collects console errors. Screenshots are attached to the Playwright result by scenario/theme;
password inputs are masked. The multi-league CI job uploads the `onboarding-acceptance` PNG artifact on success
or failure, retained for 14 days. Existing failure-only Playwright report/trace uploads are unchanged.

| Route/scenario | Expected behavior | Test/evidence checkpoint |
|---|---|---|
| Fresh `/` | Guest sees demo before authentication | `guest-demo-discovery` |
| `/leagues/demo` | Explicit invented data, no private fixture; keyboard link works | `synthetic-demo` |
| Provider selection | ESPN consent requirement and unsupported Sleeper capabilities are disclosed | `provider-limitations` |
| ESPN malformed ID | Browser blocks submission and keeps typed input | `malformed-id-kept` |
| ESPN valid but nonexistent ID | Real API refusal; correction in place | `provider-not-found` |
| Public ESPN import with existing snapshot | Only authorized ready snapshot opens dashboard | `public-ready` |
| Dashboard team selection | First decision belongs to selected league/team | `first-authorized-decision` |
| Duplicate ESPN import | Nothing added twice; existing ready league retained | `duplicate-ready` |
| Sleeper URL import | No provider cookies; correct ready dashboard link | `sleeper-ready` |
| Private ESPN form | Explanation, encrypted-storage disclosure, required consent | `private-consent-required` |
| Join another member's private league without an owned team | Actual authorization refusal, invite guidance, no cookie echo or dashboard CTA | `private-access-denied` |
| Successful private connection | Actual encrypted credential mutation, queued job, no ready CTA | `private-queued-reload` |
| Reload progress URL | Same queued state, no premature ready | `private-queued-reload` |
| Leave progress | Unpin honestly preserves membership | `leave-sync-with-membership-retained` |
| No job / queued / running | Real database state, check control, no dashboard CTA | `no_data`, `queued`, `running` |
| Overdue queued/running | Delayed wording, no invented failure | `queued_stalled`, `running_stalled` |
| Failed provider read | Honest failure and recovery controls | `failed` |
| Provider returned no teams | Empty explanation, never ready | `empty` |
| Provider access stopped | Access explanation, never ready | `access` |
| Ready but sync paused | Retained snapshot remains available with explicit older-data disclosure | `ready-with-stale-data` |
| Retry from no job | Real sync request queues a job; reload retains it | `refresh-started-real-job` |
| Retry after terminal failure | Real sync request queues a new job; no ready CTA | `failed-sync-retried` |
| Other user opens private progress | Same refusal as unknown league, no private name or ready CTA | `other-user-refused` |

## Verification and limits

- Browser matrix: all 12 journeys passed in 2.7 minutes. After adding persisted PNGs and terminal-failure retry,
  six focused checks passed in 1.7 minutes (all three 320px journeys and each width's recovery matrix).
  Record exact-head CI evidence in the PR and #167 before closure.
- `agent_check.py affected --base origin/main`: static safety, 441 affected backend tests,
  frontend lint/typecheck and 1,042 frontend unit tests passed on the final source (36.6 seconds).
- Existing `onboarding.test.ts` covers provider capability/disabled-state parsing and recovery mapping.
  `test_connect_api.py` covers backend consent, encryption, rejected credentials, authorization and refresh semantics.
- Existing `test_sync_worker.py` / `test_sync_publish.py` guard against publishing a league without teams;
  the browser fixture tests the resulting empty state, not an invented ready snapshot.
- Existing multi-league tests cover signed-out private leagues and #333's no-data pages. Existing classic-site tests
  cover the UI flag being off. Full CI must pass on the PR's exact head.

These tests intentionally do not contact ESPN/Sleeper, run a sync worker or simulate live provider reliability.
They use the real server actions, API authorization, PostgreSQL mutations and published synthetic DuckDB snapshots.
They prove readiness gating across real stored states; they do not time a worker completing or assert production deployment.
Independent review and owner-authorized integration remain separate gates. Keep #167 open until those gates pass.
