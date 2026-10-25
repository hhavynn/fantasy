# ADR-0002: Supabase control plane, verified JWTs and consented ESPN cookies

- Status: Accepted (2026-10-20), Phase 1 of the [multi-league plan](../superpowers/plans/2026-10-19-multi-league-platform.md)
- Supersedes: parts of [ADR-0001](ADR-0001-league-context-and-tenancy.md) (§2 tenancy store, §3 cache keys, §7 user
  assertion, §8 credentials) and the [roadmap](roadmap.md)
- Design: [2026-10-19-multi-league-platform-design.md](../superpowers/specs/2026-10-19-multi-league-platform-design.md)
  §1–3. Detailed task plan: [phase 1](../superpowers/plans/2026-10-19-multi-league-phase-1.md)

## Context

ADR-0001 laid a foundation for many leagues: an opaque league id, a `LeagueContext` issued only after an
authorization check, a DuckDB tenancy store, and an HMAC-signed user assertion that the Next.js server would mint
once an identity provider existed. All of it was a placeholder for a real control plane and a real identity
provider, and it stayed behind `MULTILEAGUE_ENABLED`.

The platform design (2026-10-19, revised 2026-10-20 for free tiers) picks those for real. It needs to work for
anyone on the internet, on free tiers only. Phase 1a has already moved the hosted read path off MotherDuck to an
immutable DuckDB snapshot in a private Cloudflare R2 bucket.

## Decisions

1. **Identity is Supabase Auth, verified in FastAPI.** The Next.js server forwards the user's Supabase access token
   as `Authorization: Bearer`. The API verifies it itself against the project JWKS
   (`<SUPABASE_URL>/auth/v1/.well-known/jwks.json`, cached, refreshed on an unknown `kid`): signature (`ES256`;
   `RS256` accepted for a future rotation; symmetric and `none` never), `iss = <SUPABASE_URL>/auth/v1`,
   `aud = authenticated`, `exp`, and a uuid `sub`. No header means anonymous. A header that is present but unusable
   is a 401 and is never downgraded to anonymous. `FANTASY_API_KEY` stays service authentication only. This
   replaces ADR-0001 §7: the HMAC assertion, its secret and `FANTASY_DEV_USER_ID` are deleted.

2. **The control plane is Postgres schema `fi` in Supabase project `fantasy-intel`.** It is not in schema `public`,
   because PostgREST serves `public` to anyone holding the anon key. Migrations live in
   `fantasy-intel/supabase/migrations` with down-migrations next to them. Roles follow least privilege:
   - `fi_api` (the request path) is `NOINHERIT`, a member of `anon` and `authenticated`, and has no table grants
     of its own.
   - Each request is one transaction that runs `SET LOCAL ROLE anon|authenticated` and transaction-local
     `request.jwt.claims`, so `auth.uid()` and RLS see exactly what the API verified. Both settings end with the
     transaction, which makes a Supavisor transaction-mode pool safe. A test proves two interleaved requests on
     one pooled connection never see each other's claims.
   - `fi_sync` (GitHub Actions workers) gets the sync tables only and no people tables. It reads credentials only
     through `fi.sync_credential(job_id, lease_token)`.
   - No role has `BYPASSRLS`. The Supabase service-role key is never deployed.
   - RLS is the second line of defense. Mutations that RLS cannot express (invites, admin transfer, credential
     store and revoke) go through `SECURITY DEFINER` functions. They check `auth.uid()` themselves, pin
     `search_path = ''`, and write an append-only `fi.audit_log`.

   This replaces ADR-0001 §2: the DuckDB tenancy store is deleted.

3. **Every league request is authorized server-side before anything else.** `authorize_league` runs under the
   request's role and returns an `Access` with the league's current snapshot pointer.
   - Unknown, someone else's, deleted, visibility-expired and malformed league ids are the same 404.
   - Public leagues need no sign-in, but only while the last anonymous visibility probe is younger than 24 hours
     (fail closed). Private leagues need an active membership.
   - Perspective (`team_selections`) and pins are preferences. They are never verified against league data and
     never grant anything.
   - A router-enumerated matrix test runs {anonymous, viewer, admin, non-member} against {public, private, stale,
     expired, flipped-private, deleted} on every `/v2` league route.

   This refines ADR-0001 §1. `LeagueContext` and its seal are gone, and the opaque `lg_` id stays.

4. **User-supplied ESPN cookies are accepted: opt-in, encrypted, used only by sync jobs.** ESPN offers no authorized
   third-party method, and private leagues are most leagues. A member may paste `espn_s2` and `SWID` after
   explicit consent text, which explains that the cookies grant access to their ESPN account and that signing out
   of ESPN everywhere revokes them. The encryption is an envelope:
   - AES-256-GCM under a random per-credential data key and a 96-bit nonce.
   - AAD = `"fi-cred-v1" ‖ league_id ‖ credential_id ‖ owner_user_id`. It never includes the wrapping-key version.
   - The data key is wrapped with RSA-OAEP-3072 (SHA-256) under `CREDENTIALS_PUBKEY_V<n>`.

   The API holds only public keys, so it can store a credential but never read one. The private key exists only as
   a GitHub Actions secret. Rotation rewraps the data key and leaves ciphertext, nonce and AAD byte-identical.
   Cookies are used only for GETs to that league's ESPN read endpoint, never logged, returned or put in errors.
   Ambiguous provider errors never revoke a credential or delete data. Account deletion crypto-erases a user's
   credentials (`ON DELETE CASCADE`). This replaces ADR-0001 §8 and reverses its "never collect ESPN cookies"
   alternative for the hosted product. Phase 1 ships the crypto, the table and the SQL functions. The connect
   flow that collects cookies arrives in Phase 3.

5. **Caching.** ADR-0001's `TenantScopedCache` is deleted. Phase 2 adds a per-instance response cache keyed by
   (league, data version, global version, visibility epoch, perspective, route, params). Lookups happen only after
   authorization, and league responses are `Cache-Control: private, no-store`. This replaces ADR-0001 §3.

6. **Flags are unchanged (ADR-0001 §9).** With `MULTILEAGUE_ENABLED` unset, every `/v2` path is a plain 404, Postgres
   is never opened, and production behaves as Phase 1a left it.

## Consequences

- Phase 2 serves league files through `authorize_league` (`/v2/leagues/<lg>/api/...`, `/l/<lg>/...`) with the league
  switcher and the URL perspective. Supabase sign-in in the dashboard moves to Phase 3, where Connect first needs it;
  until then the `/leagues` prototype lists providers only and perspectives come from the URL.
- Phase 3 adds sync jobs in GitHub Actions on top of `fi.sync_jobs`, `fi.provider_slots` and the fenced
  `fi.league_snapshots` pointer. It also rebuilds the connect flow on `fi.store_credential`.
- Tests need Postgres. Locally they use a throwaway `initdb` cluster with a small shim for Supabase's `auth.*`
  helpers. CI uses a `postgres:17` service container.
- The project owner sets `LOGIN PASSWORD` on `fi_api` and `fi_sync` out of band (`fantasy-intel/supabase/README.md`).
  No password is ever in a migration.

## Alternatives considered

- **Keep the HMAC assertion minted by Next.js.** This puts a second shared secret in two places and still needs a
  real identity provider behind it. Rejected in favour of verifying the provider's own tokens.
- **PostgREST with the anon key from the browser.** This exposes the control plane to anyone holding a public key
  and puts authorization in policies alone. Rejected: the control plane lives in schema `fi` and only FastAPI
  talks to it.
- **The service-role key in the API.** It bypasses RLS, so one bug would leak every league. Rejected.
- **Symmetric credential encryption with a key in the API.** An API compromise would decrypt every cookie.
  Rejected for the public-key envelope.
