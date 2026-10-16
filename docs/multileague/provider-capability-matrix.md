# Provider capability matrix (ESPN, Sleeper, Yahoo)

Status: Phase 1 research and adapter declarations. Terms and rate-limit research dated **2026-10-16**.
Code: `fantasy-intel/backend/app/leagues/providers/` (`espn.py`, `sleeper.py`, `yahoo.py`, `registry.py`).
The matrix below is written from the adapters' own `capabilities()` declarations; the tests in
`tests/test_league_providers*.py` check that every capability is declared and that this document has a row for each.

## How this was researched, and what could not be verified

The research environment's egress policy blocked the primary sources: `docs.sleeper.com`,
`support.sleeper.com`, `sports.yahoo.com`, `developer.yahoo.com`, `legal.yahoo.com` and `espn.com` could not be
fetched directly (connection refused by the proxy / DNS). What follows therefore rests on:

* **search-result excerpts of the primary pages** (marked *via search excerpt*): the page's text was quoted by
  the search tool but the page itself was not opened;
* **GitHub** pages and issues, which were reachable (marked *GitHub*);
* **the installed `espn-api` package source** in this repo's virtualenv (marked *source*);
* **community libraries and mirrors** of the Sleeper docs (marked *secondary*).

Anything not backed by one of these is marked **unverified**. Nothing here is legal advice, and the licensing
rows are *not* resolved: see "Unresolved before public launch".

## Capability matrix

Each cell is `support in this deployment` / what the provider offers. "Default deployment" means no feature
flags set, no ESPN cookies in the environment, no `YAHOO_CLIENT_ID`: **all three adapters are then disabled**
and every capability reads `disabled` (`ProviderCapabilities.state`). The `when enabled` column states what
the adapter declares once a flag is on. Support values are `SUPPORTED`, `DERIVED` (we compute it from other
supported data), `UNSUPPORTED`, `REQUIRES_AUTH`, `DISABLED`.

Enabling: Sleeper `SLEEPER_SOURCE=fixture|live`; ESPN `ESPN_PUBLIC_IMPORT_ENABLED=1` (public leagues) or the
owner's `ESPN_S2`/`SWID` in the server environment (owner's own leagues); Yahoo cannot be enabled in Phase 1.

| Capability | ESPN (when enabled) | Sleeper (when enabled) | Yahoo (Phase 1) |
|---|---|---|---|
| `discover_leagues` | UNSUPPORTED. No documented discovery endpoint; leagues are connected by id. (A cookie-based "fan" API that lists a user's leagues exists in the community libraries; not used.) | SUPPORTED. `GET /user/{username}`, then `GET /user/{user_id}/leagues/nfl/{season}`. A Sleeper username is public, so listing proves nothing about who is asking. | DISABLED. Yahoo offers the signed-in user's leagues (users / games / leagues collections). |
| `league_settings` | SUPPORTED. `mSettings` view. Undocumented API. | SUPPORTED. `GET /league/{id}`: `roster_positions`, `settings`, `scoring_settings`. | DISABLED. League settings resource. |
| `teams` | SUPPORTED. `mTeam`; manager ids are one-way hashes (`espn:sha256:<12 hex>`), never SWIDs. | SUPPORTED. Teams are rosters; `external_team_id = str(roster_id)`; managers `sleeper:<user_id>` (owner and co-owners). | DISABLED. League teams resource. |
| `rosters` | SUPPORTED. `mRoster`; ids `espn:<id>`; GSIS identity is left to the caller (resolved from the analytics `player_id_map`, id-based). | SUPPORTED. `/league/{id}/rosters` plus the cached player list; ids `sleeper:<id>`; GSIS links from the DynastyProcess ID map (high), Sleeper's own `gsis_id` (medium), team defenses, conflicts and unmatched kept with reasons. | DISABLED. Team roster resource; ids would be `yahoo:<player key>`. |
| `scoring_rules` | SUPPORTED. `scoringItems` verbatim by stat id; known ids mapped to canonical stats; per-slot `pointsOverrides` (how ESPN expresses e.g. a TE premium) kept as `<stat>@slot<id>` and reported unmapped. | SUPPORTED. `scoring_settings` verbatim; known keys mapped (`bonus_rec_te` -> `rec_te_bonus`); the rest listed as unmapped. A league with no `rec` key has no PPR value; nothing defaults to PPR. | DISABLED. Stat modifiers inside league settings. |
| `matchups` | SUPPORTED. `mMatchup`; week = matchup period. | SUPPORTED. `GET /league/{id}/matchups/{week}`: rows sharing a `matchup_id` play each other. Sleeper cannot tell an unplayed week from 0 points (unverified for future weeks). | DISABLED. Scoreboard / team matchups. |
| `standings` | SUPPORTED. `mStandings` (playoff seed used as rank). | DERIVED. No standings endpoint: ranked by wins, ties, points for from the rosters' records. League tiebreakers and divisions are not applied. | DISABLED. Standings resource. |
| `free_agents` | SUPPORTED. `kona_player_info` with `FREEAGENT` status; players on waivers are excluded. | DERIVED. No free-agent endpoint. Active NFL players at QB/RB/WR/TE/K/DEF who are on no roster of this league (from the once-a-day player list). Waiver-locked players are not distinguished. | DISABLED. Players collection filtered to `status=A` (available); the filter value comes from community libraries, **unverified**. |
| `transactions` | SUPPORTED. `mTransactions2`; executed adds, drops and trades only. | SUPPORTED. `GET /league/{id}/transactions/{week}` (`leg` is the week); completed transactions only; draft picks and FAAB bids not carried. | DISABLED. Transactions resource. |
| `projections` | SUPPORTED. ESPN's own per-week projection under the league's scoring (`kona_player_info`). Redistribution rights unresolved (see below). | UNSUPPORTED. No projections endpoint in the documented API (absence **unverified**: primary docs not opened). | UNSUPPORTED. Not confirmed in Yahoo's documentation (**unverified**). |
| `private_leagues` | REQUIRES_AUTH. "owner deployment only; no approved third-party authorization method". | UNSUPPORTED. "Sleeper leagues are readable by id; Sleeper has no per-user private access model in its public API." | REQUIRES_AUTH. Yahoo leagues are private to members; reading one needs that member's OAuth token. |

`is_public` on `LeagueSettings`: Sleeper reports `True`, meaning only that the provider serves the league
without credentials to anyone holding the id. It does not say the league is listed anywhere or that the
connecting user belongs to it. ESPN reports `settings.isPublic` when present, otherwise `None`.

## Access and authorization model

| | ESPN | Sleeper | Yahoo |
|---|---|---|---|
| Official public API | None found. The fantasy v3 API (`lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/...`, *source*) is undocumented and used by community libraries. | Yes, documented: `https://api.sleeper.app/v1`, read-only. | Yes, documented Fantasy Sports API (`fantasysports.yahooapis.com`, *secondary*). |
| Auth | None for public leagues (community reports, *secondary*). Private leagues need the user's `espn_s2` and `SWID` browser session cookies; they cannot be obtained programmatically (*secondary*, ffscrapr vignette). **No OAuth.** | None. No token; "no content can be modified" (*secondary*). | OAuth 2.0 authorization code flow against `api.login.yahoo.com/oauth2/request_auth` and `/oauth2/get_token` (*via search excerpt* of Yahoo's OAuth2 guide). Read scope named `fspt-r` by community projects (**not found on an official Yahoo page**, verify). |
| Token lifetime | n/a (cookies) | n/a | Access token 1 hour; refresh token via `get_token`; the server may rotate the refresh token (store the new one); the user can revoke in Yahoo account settings (*via search excerpt*). |
| Developer registration | n/a | None. | Application with Yahoo: organization, product, use case, data needed, intended user base (incl. "personal or single league use"); manual review; the developer must sign the "API Access and Use Agreement"; read-only unless a unique use case is justified; incomplete applications are closed without reply (*via search excerpt* of `sports.yahoo.com/developer/access/`). |
| What this deployment does | Owner's own leagues via server-side cookies (`AccessMethod.OWNER_ENV`); public leagues only if `ESPN_PUBLIC_IMPORT_ENABLED`. Cookies are never collected from users, logged, or put in errors/reprs. | `AccessMethod.PUBLIC`; any credential passed in is ignored and never stored. | Nothing: always disabled. |
| Known operational risk | Undocumented, can change without notice; `espn_api` sets no HTTP timeout (*source*). | Rate-limit figures disagree between sources (below). | GitHub issue [uberfastman/yfpy#84](https://github.com/uberfastman/yfpy/issues/84) (*GitHub*, user reports, checked 2026-10-16): since about 2026-07-22 API calls return 403 "This application is not authorized" on all endpoints even though OAuth sign-in and refresh still work; new app registrations could not select Fantasy Sports permissions; apps created before access was enabled reportedly cannot be fixed and need a new app/Client ID; review time reported as 1-2 weeks up to over a month. Single unofficial source: **unverified**, but it means Yahoo access must be proven with a live call before any build. |

## Rate limits and etiquette

| Provider | Stated limit | Source and confidence | What the code does |
|---|---|---|---|
| Sleeper | "stay under 1000 API calls per minute, otherwise you risk being IP-blocked" | Go client README and a docs mirror (*secondary*). A Postman collection lists **90 requests/minute per IP** instead. **Unverified; sources conflict.** | `SleeperClient`: 60 s in-memory cache for league/users/rosters/state; `/players/nfl` (about 5 MB) at most once per 24 h, persisted on disk; 429/5xx retried with `Retry-After` or exponential backoff (max 3 retries). |
| Sleeper players endpoint | "You do not need to call this endpoint more than once per day." | *via search excerpt* of `docs.sleeper.com`. | As above. Fixture mode uses a temp cache dir and never the live one. |
| Yahoo | Terms: a rate limit applies per application, set in the API documentation, may change at Yahoo's discretion; programmatic circumvention is a violation. Yahoo may throttle excessive use. The developer page (YQL era) mentions 20,000 signed and 2,000 unsigned calls per hour. | *via search excerpt* of `legal.yahoo.com/.../fantasysportsapi` and the developer page. The hourly figures are dated: **unverified for the current API**. | Not applicable (disabled). |
| ESPN | None documented. | No official source. | Existing `EspnClient`; the multi-tenant path must add its own throttling before the public flag is used. |

## Licensing and terms status (checked 2026-10-16)

| Provider | What was found | Status |
|---|---|---|
| Sleeper | No API-specific terms or commercial licence were found. Sleeper's [General Terms of Use](https://support.sleeper.com/en/articles/5486620-general-terms-of-use) (*via search excerpt*) distinguish an **"Approved Integration Partner"** (a party with a separate written agreement or Sleeper's approval) and say a user who connects a third-party product agrees to that third party's terms. The [Privacy Notice](https://support.sleeper.com/en/articles/5486618-privacy-notice) says linked platforms receive profile and league data. [docs.sleeper.com](https://docs.sleeper.com/) describes the API as read-only and token-free. A consultancy blog (*secondary*, not Sleeper) says commercial use should be reviewed and may involve a partner relationship. | **Unresolved.** Whether a commercial multi-user product may use the public API, and whether we must be an Approved Integration Partner, is not established. Live import therefore stays off (`SLEEPER_SOURCE=disabled` by default; the adapter's disabled reason says so). |
| Yahoo | Access requires an approved application and signed API Access and Use Agreement ([Yahoo Fantasy Sports API portal](https://sports.yahoo.com/developer/), [apply](https://sports.yahoo.com/developer/access/), *via search excerpt*). [Yahoo Fantasy Sports APIs: Terms of Use](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/fantasysportsapi/index.html) (*via search excerpt*) cover the rate limit and its circumvention. The portal says products must show the attribution "Fantasy data provided by Yahoo Fantasy" linking back to Yahoo Fantasy. Read-only by default. The agreement's full text (storage, caching, redistribution, commercial limits) was **not read**. | **Unresolved.** No approved app exists; the agreement has not been read or signed; storage/caching/commercial terms unknown. |
| ESPN | No ESPN developer terms for the fantasy API exist (no official API). ESPN's site Terms of Use refer to Disney's terms; a scraping-vendor blog claims they bar automated access (*secondary, not a quote*). The wording was **not obtained**. | **Unresolved.** Using an undocumented API for other people's leagues has no granted right that we can point to. This is why public import is a default-off flag and private leagues are owner-only. |

## Phase 1 capability decisions (summary)

* **Sleeper `free_agents` = DERIVED, not UNSUPPORTED.** Availability can be computed (all active fantasy players
  minus every roster in the league) from official endpoints, and the note says what it cannot see (waivers, locks).
  If product review decides an approximate list is too misleading for waiver advice, flip the declaration to
  `UNSUPPORTED`; nothing else changes.
* **Sleeper `standings` = DERIVED** from roster records, with the ordering rule stated.
* **ESPN `projections` = SUPPORTED** because ESPN publishes them inside the same undocumented API, but treat the
  redistribution question as open.
* **ESPN `discover_leagues` = UNSUPPORTED** (we deliberately do not use the cookie-based fan API).
* **Yahoo is always disabled** with the two required reasons; its per-capability notes describe what Yahoo offers
  and are not a promise for this deployment.
* **Fixture mode** (`SLEEPER_SOURCE=fixture`) serves invented, recorded-shape files from
  `app/leagues/providers/fixture_data/` (inside the application package so it works from a deployed build;
  `tests/**` is excluded from the Vercel bundle). Results carry provenance `fixture:sleeper:...` and
  `data_origin == "fixture"`. The base league files are kept byte-identical with `tests/fixtures/sleeper`
  (enforced by a test). The matchup, transaction and user fixtures were written from the documented response
  shapes and are **not recorded from live responses**.

## Unresolved before public launch

1. **Sleeper usage rights.** Obtain written confirmation (Sleeper terms or partner agreement) that a
   multi-user, possibly paid product may read the public API and display league data; find out whether
   Approved Integration Partner status is required. Until then `SLEEPER_SOURCE` stays `disabled` in production.
2. **Sleeper rate limit.** Reconcile 1000/min (secondary) with 90/min per IP (Postman); serverless egress IPs
   are shared, so per-IP limits matter. Add per-tenant throttling and a global budget before enabling `live`.
3. **Sleeper identity privacy.** Anyone can connect any league by id or username: decide whether that is
   acceptable, and review how other managers' display names and user ids (third-party personal data) are
   stored and shown.
4. **ESPN terms.** Read ESPN's and Disney's current Terms of Use for automated access to the undocumented fantasy
   API; decide whether `ESPN_PUBLIC_IMPORT_ENABLED` may ever be turned on for third parties. Private leagues
   stay owner-only (no approved third-party authorization exists, and session cookies are never collected).
5. **ESPN operational risk.** The API can change without notice; `espn_api` has no request timeout (a hung
   connection can block a worker); add timeouts and per-tenant throttling before any public use.
6. **ESPN projections redistribution.** Whether ESPN's projections may be shown to other users is unknown.
7. **Yahoo access.** Apply for the developer app, read and sign the API Access and Use Agreement, then prove
   access with a real call (the July 2026 403 reports in yfpy#84 suggest the approval flow is changing).
   Confirm the read scope name (`fspt-r` is from community code), the `status=A` free-agent filter, whether
   projections exist, and the current rate limits.
8. **Yahoo implementation prerequisites.** OAuth sign-in, encrypted token storage (refresh-token rotation),
   revocation on disconnect, the required attribution text, and any caching or retention limits in the agreement.
9. **Player-id map licence.** The Sleeper identity links rely on the DynastyProcess / nflverse ID map; confirm
   its licence permits this use (unverified here).
10. **Live-shape verification.** Re-record Sleeper matchup (`points` for unplayed weeks, bye rows with
    `matchup_id` null) and transaction (`status`, `leg`, `adds`/`drops`, `waiver_bid`) responses against the live
    API once item 1 allows it, and check Sleeper's `waiver_type` and `trade_deadline` value meanings (the
    foundation code assumes 0 rolling / 1 reverse standings / 2 FAAB; **unverified** against the primary docs).
11. **Privacy policy and consent text** covering what is read from each provider and how long it is kept.
