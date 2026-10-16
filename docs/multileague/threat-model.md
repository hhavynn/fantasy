# Threat model: multi-league, multi-provider Fantasy Intelligence

- Date: 2026-10-16 (Phase 1 foundation)
- Method: STRIDE over the data flows below, focused on tenant isolation and provider credentials.

## Assets

| Asset | Sensitivity |
|---|---|
| Provider credentials: owner ESPN cookies (`espn_s2`, `SWID`), future Yahoo OAuth access/refresh tokens | Critical: full account access at the provider |
| Private league data (rosters, settings, member names, transactions) | Confidential to that league's members |
| Per-user state (connected leagues, chosen team, cached personalized results) | Confidential to the user |
| Service secrets: `FANTASY_API_KEY`, `FANTASY_USER_ASSERTION_SECRET`, `MOTHERDUCK_TOKEN` | Critical: impersonation / data-plane access |
| Public league data (Sleeper by id, ESPN public leagues), nflverse statistics | Public, but integrity matters |

## Trust boundaries and data flow

```
Browser ──(session; SITE_PASSWORD today)──> Next.js server ──(X-API-Key + X-Fantasy-User-Assertion)──> FastAPI
                                                                                    │
                       ┌────────────────────────────────────────────────────────────┼──────────────────────┐
                       ▼                                                            ▼                      ▼
             Tenancy store (separate DB:                              Analytics DuckDB / MotherDuck    Provider APIs
             users, connections, snapshots,                           (owner's ESPN league; published   (ESPN, Sleeper,
             credential *refs*)                                        wholesale; NOT tenant-safe)       Yahoo)
```

1. Browser → Next.js: untrusted input. 2. Next.js → API: service-authenticated; the user is
asserted by a signed, short-lived token. 3. API → providers: outbound, credentials per call.
4. API ↔ stores.

## Threats and mitigations

| # | STRIDE | Threat | Phase 1 mitigation | Residual / next |
|---|---|---|---|---|
| T1 | Elevation | **Horizontal privilege escalation**: user A changes `league_id` in a URL/body to read B's private league | Every `/v2/leagues/{id}` route authorizes via `store.authorize(principal, id)` → `LeagueContext`; requires an active connection for *this* principal; unknown and not-yours both 404 | Legacy endpoints are single-owner only; they must not be exposed to other users until migrated (Phase 2) |
| T2 | Spoofing | Caller sets `user_id` themselves | Identity only from HMAC-verified assertion (audience, ≤10 min TTL, 60 s skew, constant-time compare); no user id parameter exists | Needs a real IdP (Phase 2); secret rotation procedure |
| T3 | Spoofing | `FANTASY_API_KEY` holder impersonates users | API key is service auth only; `/v2` user routes require the assertion in addition | Key and assertion secret must be different values and stored only server-side |
| T4 | Info disclosure | Cache poisoning / cross-tenant cache hits | Keys embed tenant hash, provider, internal league id, season, data version; cache API requires a `LeagueContext` | Existing trade-discovery cache is single-tenant; migrate with its engine |
| T5 | Info disclosure | Missing league filter or unscoped join returns other leagues' rows | Store methods take a context and filter by `league_id`; tests seed two leagues and assert no bleed; ratchet test pins legacy global selectors | Analytics tables lack league keys (migration proposal) |
| T6 | Info disclosure | Id collisions: same external league id on two providers, same team id in two leagues, same player name | `LeagueKey` includes provider; `Team.scoped_id` includes league; player ids namespaced; no name matching | — |
| T7 | Info disclosure | Credentials leak via logs, exceptions, `repr`, JSON, git | Credentials never stored for users; owner cookies wrapped with redacting `repr`; sync errors sanitized and length-capped; tests scan the repo and captured logs | Encrypting vault + KMS before any user credential (Yahoo) |
| T8 | Spoofing / Elevation | Credential harvesting UX (asking users for ESPN cookies) | Not implemented by design; no cookie/password/token inputs exist; ESPN private leagues are owner-only | Revisit only if ESPN offers an authorized method |
| T9 | Info disclosure | Publishing tenant data to the shared MotherDuck DB | Tenancy store is a separate file; `publish.py` copies only the analytics file | Per-tenant scoping of the hosted data plane (Phase 3) |
| T10 | Elevation | Connecting a private league with `PUBLIC` access to bypass credentials | Store rejects `PUBLIC` unless the provider reports the league public; owner-env access only for the configured owner user on ESPN | — |
| T11 | Info disclosure | Enumeration of internal league ids | Opaque 144-bit random ids; 404 on miss | Rate limiting at the edge (Phase 2) |
| T12 | DoS | One provider's outage/rate limit breaks other tenants | Failures recorded per league only; reads come from stored snapshots, not live calls; Sleeper client backs off on 429/5xx | Per-provider circuit breaker and job queue (Phase 2) |
| T13 | Tampering | Revoked connection keeps working through a held context | Store re-checks the connection on every read; revoke marks credential refs revoked | Provider-side revocation for OAuth (Phase 2) |
| T14 | Repudiation | No audit trail of connects/revokes | Timestamps on connections and credential refs | Append-only audit log (Phase 2) |
| T15 | Tampering | CSRF on connect/revoke | Mutations are Next.js server actions (same-origin, POST, framework action ids); the backend is not browser-reachable without the service key | Add explicit CSRF/origin checks when real sessions exist |
| T16 | Info disclosure | Anonymous access | Anonymous principals hold no league context: `/v2/leagues` 401, league routes 404; only `/v2/providers` (public capability info) is anonymous | — |
| T17 | Legal | Using provider data outside its terms | Live Sleeper import off by default; ESPN public import off; Yahoo disabled until app approval | Resolve terms before launch (capability matrix) |

## Explicit non-goals in Phase 1
- No production identity provider, no user credential storage, no live provider calls by default,
  no change to the hosted owner deployment.

## Verification
`tests/test_multileague_isolation.py`, `tests/test_leagues_*.py`, `tests/test_league_providers*.py`,
`tests/test_no_credential_leaks.py`, `tests/test_league_scope_audit.py`.

## Review findings (Phase 1 adversarial review, all fixed in this PR)

| Id | Severity | Finding | Fix | Regression test |
|---|---|---|---|---|
| SEC-1 | Medium | A `PUBLIC` connection kept authorizing after the league was re-registered as private, exposing snapshots refreshed with owner credentials | Every authorization re-checks `access_method = public ⇒ league is_public` in SQL (`store._ACCESS_STILL_VALID`) | `test_sec1_public_connection_stops_authorizing_when_the_league_turns_private` |
| SEC-2 | Medium | An `OWNER_ENV` connection survived a change/removal of `FANTASY_OWNER_USER_ID` | Same predicate: `owner_env ⇒ provider = espn AND user = current owner` | `test_sec2_owner_env_connection_ends_when_the_user_is_no_longer_the_owner` |
| SEC-3 | Low | With the flag off, `/v2` answered 405/307 for other methods and slash variants, revealing the routes | Middleware answers 404 for every `/v2` path and method while disabled | `test_sec3_flag_off_is_404_for_every_method_and_path_shape` |
| SEC-4 | Low | `sanitize_error` kept the value after `Authorization: <scheme>` | Whole authorization line redacted; OAuth `code`/`state` parameters redacted | `test_sec4_...` |
| SEC-5 | Low | `dataclasses.replace()` could forge a `LeagueContext` (trusted by the tenant cache) | Contexts carry a per-process HMAC seal over league, key, principal, role and access | `test_sec5_a_context_cannot_be_forged_by_replacing_its_principal_or_league` |
| SEC-6 | Low | The assertion secret could equal `FANTASY_API_KEY` (sent on every request) | Treated as unset when equal | `test_sec6_...` |
| SEC-7 | Low/Medium | `repr(Settings)` printed `espn_s2`, `SWID`, the API key and the assertion secret (pre-existing for the first three) | Those fields are `repr=False` | `test_sec7_settings_repr_does_not_print_credentials` |
| SEC-8 | Low | Adapters re-raised unrecognised exceptions with provider text and chains | Both adapters convert any exception into a fixed-text `ProviderUnavailable` without `__cause__`/`__context__` | `test_sec8_...` |

Accepted / informational (no code change in Phase 1): connect has no per-user rate limit and `espn_api`
sets no HTTP timeout (Phase 2: timeouts, per-user caps, circuit breaker); any signed-in user can read any
Sleeper league by id (Sleeper's own model); anonymous `/v2/providers` reveals whether owner ESPN cookies
are configured (not their values); `FANTASY_DEV_USER_ID` makes every visitor one user on non-production
servers; `FANTASY_OWNER_USER_ID` must be an unguessable IdP subject once sign-in exists.
