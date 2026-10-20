"""Supabase control plane: authorization, preferences and the sync queue for the multi-league platform.

Nothing here runs unless MULTILEAGUE_ENABLED is on; the legacy single-league routes never import it. See
docs/multileague/ADR-0002-supabase-control-plane.md and fantasy-intel/supabase/migrations/.

  pg                 - ControlPlane: one Postgres transaction per request as anon|authenticated with verified claims
  jwt_auth           - Supabase access-token verification (JWKS) -> Principal
  authz              - authorize_league: visibility x membership x status, returns the snapshot pointer
  mutations          - pins, perspective, and wrappers for the SECURITY DEFINER functions
  credential_crypto  - envelope encryption for user-supplied ESPN cookies (API holds public keys only)
"""
