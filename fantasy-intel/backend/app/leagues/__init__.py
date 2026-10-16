"""Multi-league, multi-provider foundation (Phase 1).

Nothing in this package is used by the existing single-owner ESPN endpoints. It is reached only
through the feature-flagged `/v2` routes (MULTILEAGUE_ENABLED) and its own tests.

Layers, top to bottom:
  * context       - who is asking (Principal) and which league they may act on (LeagueContext).
                    A LeagueContext is only ever issued by an authorization check.
  * models        - provider-neutral league settings, lineup rules, teams and rosters.
  * identity      - namespaced provider player ids and their (evidence-only) GSIS links.
  * capabilities  - what each provider can and cannot supply, stated explicitly.
  * providers     - one adapter per fantasy platform behind the LeagueProvider interface.
  * store / cache - tenant-scoped persistence and cache keys (separate from the analytics DB).

See docs/multileague/ADR-0001-league-context-and-tenancy.md.
"""
