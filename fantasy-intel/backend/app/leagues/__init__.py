"""Multi-league, multi-provider foundation: names and provider adapters.

Nothing in this package is used by the existing single-owner ESPN endpoints. Authorization, preferences and
credential storage live in app/control_plane (Supabase); this package only describes leagues.

  * context       - Provider, LeagueKey, Principal (the verified token's claims), AccessMethod.
  * models        - provider-neutral league settings, lineup rules, teams and rosters.
  * identity      - namespaced provider player ids and their (evidence-only) GSIS links.
  * capabilities  - what each provider can and cannot supply, stated explicitly.
  * providers     - one adapter per fantasy platform behind the LeagueProvider interface.
  * credentials   - redacting value types for cookies (`Secret`, `EspnCookies`).
  * redaction     - `sanitize_error` for anything provider- or credential-adjacent.

See docs/multileague/ADR-0001-league-context-and-tenancy.md and ADR-0002-supabase-control-plane.md.
"""
