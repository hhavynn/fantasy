# Multi-league Phase 6: launch gates, then open sign-ups — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the multi-league site can be opened to anyone. It has published terms and a privacy policy, abuse limits, a free-tier usage dashboard with alerts, dead-job alerts and tested deletion flows. Sign-ups open at the end.

**Architecture:**
- **Postgres functions** enforce the per-account limits and the server-side sign-up gate, and perform account deletion.
- **The web proxy** rate-limits anonymous page reads. A Vercel WAF rule backs it up.
- **A scheduled GitHub workflow** samples usage into Postgres and opens GitHub issues for alerts. Issues are free, and GitHub emails Havyn.
- **An admin page** shows the latest samples.

**Tech Stack:** Postgres (Supabase), FastAPI, Next.js proxy, GitHub Actions + `gh`, Cloudflare R2 (S3 API), Vercel WAF.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md) (Phase 6).

## Global Constraints

- **Free tiers only.** Vercel Hobby includes one WAF rate-limit rule per project and 1M allowed requests.
- **Every request is authorized server-side first.** Unknown and unauthorized leagues are both 404. The admin usage page answers 404 to anyone who is not a platform admin.
- **No service-role key is deployed.** Account deletion runs through a `SECURITY DEFINER` function that deletes only the caller's own `auth.users` row.
- **Logs and issues are public** (public repo). Monitor output and alert issues carry only metric names, numbers, job ids and error codes. They never carry league names, emails or provider data.
- **Flags keep today's behavior.** With every new flag at its default, production behaves as Phase 5 left it. The sign-up gate defaults closed, which is today's state: the web form already refuses new accounts.
- Los Subies goldens stay green. The rollback path stays.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Terms and privacy | Havyn approved launch on 2026-10-20 ("goahead is given"). See below for what the terms say | Spec: non-commercial (Vercel Hobby); ESPN's API is undocumented; Sleeper's API is public and read-only |
| Contact | GitHub issues on the public repo. No email is published | Keeps Havyn's address off the site |
| Sign-up gate | API `SIGNUPS_ENABLED` (default off). Every request transaction sets the GUC `fi.signups_open`; `fi.ensure_app_user` refuses to create a new account when it is `off`. The API answers 403 "Sign-ups are not open yet." The web form keeps `SIGNUPS_OPEN` | Someone can call Supabase Auth directly with the publishable key, so the web flag alone does not stop sign-ups. This gate means a new account gets no data access until sign-ups open |
| Active leagues per account | 25: pins plus live memberships. Enforced in `import_public_league`, `connect_private_league` and pinning. Error `league_limit` → 409 | Bounds the sync work one account can cause. The existing 20 registrations per day stays |
| Read rate limit | `RATE_LIMIT_PER_MINUTE` (off when unset; 300 at launch): the web proxy counts every request per client IP per instance and answers 429 with `Retry-After`. Signed-in visitors are counted too: the proxy does not verify the session cookie, so a forged one must not buy a higher limit. Backed by one Vercel WAF rule on the web project (600 requests/60 s per IP → 429) | The API is reachable only with the web's key, so the web is the edge. The in-code limit is testable; the WAF rule covers every instance and region |
| Usage budgets (70% alert) | Sampled hourly:<br>• **Supabase:** DB size (500 MB) and accounts (50k MAU proxy).<br>• **R2:** storage (10 GB) and object count from a listing.<br>• **R2 operations:** Class A 1M and Class B 10M per month, needs `CLOUDFLARE_ANALYTICS_TOKEN`.<br>• **Vercel:** Active CPU (4 h), invocations (1M) and provisioned memory (360 GB-h), from `/v1/billing/charges`, needs `VERCEL_USAGE_TOKEN`.<br>A source without its token reports "not configured". Supabase egress has no API on the free plan; Supabase's own usage emails cover it | Every number comes from a free API. Optional tokens are Havyn's account actions |
| Alerts | A GitHub issue per alert kind, labelled `platform-alert`. A new metric or new dead job id adds a comment, and GitHub emails Havyn. The issue is closed when the condition clears | Free; email included; no new service |
| Dead jobs | The same hourly monitor lists `dead` jobs from the last 7 days: id, league id, kind, error code | Spec §3.6 says to alert on `dead` |
| Account deletion | `DELETE /v2/me` → `fi.delete_my_account()` deletes the caller's `auth.users` row. Phase 1's cascade and rules trigger do the rest: credentials crypto-erased, memberships, pins, selections and invites removed, heir promoted, empty league soft-deleted. The `/account` page asks for the typed word DELETE, then signs out | One path, already tested at the SQL level |
| Takedown | Runbook only: `UPDATE fi.leagues SET deleted_at = now() WHERE id = …` in the Supabase SQL editor. Reads stop at once, and the purge job deletes the objects after 7 days | Rare; there is no admin UI to build |

## What the terms say

- **Free and non-commercial.** No ads and no payments. Unaffiliated with ESPN, Sleeper or the NFL.
- **ESPN.** Reads use ESPN's undocumented API, which can change or stop. Private leagues are connected only with the member's own cookies, which are encrypted and used only to read that league.
- **Sleeper.** Data comes from Sleeper's public read-only API.
- **No guarantees.** Projections are estimates, provided as is, with no warranty.
- **Acceptable use.** No scraping and no abuse. Accounts or leagues can be removed.

## What the privacy policy says

- **Stored:** email (sign-in), league ids, pins, team choices, encrypted ESPN cookies, and an audit log of actions (metadata, kept 1 year).
- **Processors:** Supabase, Cloudflare R2, Vercel and GitHub Actions.
- **Not done:** no selling, no ads, and no third-party tracking.
- **Deletion:** in-app, which crypto-erases cookies immediately. League files are deleted 7 days after a soft delete.

## Status

Tasks 1–4 are built:
- Terms and privacy pages, linked from sign-in, Connect and the account menu.
- The sign-up gate (`SIGNUPS_ENABLED`, default closed).
- The 25-league cap: a trigger on pins and memberships, so every insert path is covered.
- Account deletion (`DELETE /v2/me`, `/account`), tested from the API down to the purge of the league's objects.
- The read rate limit (`RATE_LIMIT_PER_MINUTE`, off when unset).
- The platform monitor (hourly workflow, GitHub issue alerts, `/admin/usage`).

The monitor's log is checked by `scripts/audit_drain_log.py --monitor`, an allowlist of its own line shapes. The Vercel and Cloudflare collectors are tested against the documented response shapes only; they run once Havyn adds `VERCEL_USAGE_TOKEN` and `CLOUDFLARE_ANALYTICS_TOKEN`. Task 5 (launch) follows the merge.

## Tasks

### Task 1: Terms, privacy and the acknowledgement
- `frontend/app/terms/page.tsx`, `frontend/app/privacy/page.tsx`: static pages using the site's page styles.
- `frontend/lib/league-scope.ts`: add `/terms` and `/privacy` to `UNSCOPED_PREFIXES`, with a test.
- The login form says "By signing in you agree to the Terms and the Privacy policy", with links. The Connect form links the Privacy policy next to the consent checkbox. The account menu links both.
- Test: `lib/no-coming-soon.test.ts` stays green. A unit test checks that the proxy leaves `/terms` and `/privacy` unscoped.

### Task 2: Sign-up gate, league cap, account deletion (one migration)
- `supabase/migrations/20261022120000_launch_gates.sql`, plus a down-migration:
  - `fi.ensure_app_user` refuses a new row when `current_setting('fi.signups_open', true) = 'off'` (`signups_closed`).
  - `fi.check_active_leagues(uuid)` enforces the cap of 25 (`league_limit`). It is called by `import_public_league` (only when the caller has no pin on that league yet), by `connect_private_league` and by the new `fi.pin_league(text)`.
  - `fi.delete_my_account()`.
- `app/control_plane/pg.py` sets `fi.signups_open` from `SIGNUPS_ENABLED`. `config.signups_enabled` defaults off.
- API:
  - 403 for `signups_closed` (connect, import, pins, perspective, invites, `/v2/me`);
  - 409 for `league_limit`;
  - `DELETE /v2/me` → 204.
- Web: `/account` page (email, delete form) and an "Account settings" link in the account menu.
- Tests:
  - Postgres function tests: the gate on/off/unset, the cap at 25 including re-import of a pinned league, and `delete_my_account` erasing credentials.
  - API tests: 403, 409, `DELETE /v2/me`.
  - A deletion-flow test: through the API and the purge job, the league objects go 7 days later.

### Task 3: Anonymous rate limit
- `frontend/lib/rate-limit.ts`: a pure fixed-window counter keyed by IP, with a bounded map, and `limitFromEnv`.
- `proxy.ts` applies it right after the site password, and answers 429 with `Retry-After`.
- Unit tests: the limit, the window reset, the map bound, the IP header order and the env switch.
- After merge, add the Vercel WAF rule on the web project through the API (`PUT /v1/security/firewall/config`).

### Task 4: Platform monitor
- `supabase/migrations/20261022130000_usage_samples.sql`:
  - `fi.usage_samples(metric, used, quota, unit, sampled_at)`. `fi_sync` may insert and select; rows older than 90 days are pruned.
  - `fi.usage_overview()` returns the latest sample per metric and is for platform admins only. `fi.dead_jobs(interval)` is for `fi_sync`.
- `backend/app/monitor/`:
  - `collect.py`: one collector per source; each returns samples or "not configured".
  - `alerts.py`: a pure function from samples and dead jobs to alert states.
  - `github.py`: open, update or close issues through `gh`, deduplicated by a state marker in the issue body.
- `scripts/platform_monitor.py`: `--inject metric=used/quota` forces a sample, to prove that an alert fires.
- `.github/workflows/platform-monitor.yml`: hourly plus `workflow_dispatch` (inject input). Never on PRs. Permissions are `issues: write` and `contents: read`. It uses the existing sync secrets plus the optional tokens.
- `GET /v2/admin/usage` in `leagues_v2.py` (platform admin, else 404) and `frontend/app/admin/usage/page.tsx` (`/admin` is unscoped).
- The workflow audits its own log with `scripts/audit_drain_log.py --monitor`.
- Tests:
  - collectors with fake clients;
  - alert thresholds at 69.9% and 70%;
  - the dedupe and closing logic with a fake `gh`;
  - the admin route returns 404 for a non-admin;
  - the public-output audit: no league names or emails.

### Task 5: Launch
- Merge, then set `SIGNUPS_ENABLED=1` (API) and `SIGNUPS_OPEN=1` (web).
- Set `RATE_LIMIT_PER_MINUTE=300` and remove `SITE_PASSWORD` on the web project, so the site is public.
- Add the WAF rule. Redeploy both.
- Run the monitor once by hand, with an injected threshold, and confirm the issue opens and then closes.
- Rollback: `SIGNUPS_ENABLED=0` closes registration. `MULTILEAGUE_ENABLED=0` closes public viewing. Restoring `SITE_PASSWORD` closes the whole site.
