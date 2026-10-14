"""Which external provider feeds player_week_usage for a season.

There is deliberately no built-in brand ranking. Rules:
  * no external rows                       -> none
  * exactly one provider                   -> use it
  * several providers + configured preference present -> use the preference
  * several providers otherwise            -> use NONE and surface a data-quality warning
    (values from different providers are never silently merged)
Preference comes from EXTERNAL_USAGE_PREFERRED_PROVIDER (e.g. set by a private adapter).
"""

from __future__ import annotations

from ..db.connection import table_exists


def select_provider(con, season: int, preferred: str | None = None) -> dict:
    if not table_exists(con, "external_player_week_usage"):
        return {"provider": None, "status": "none", "providers": [], "message": "no external usage imported"}
    providers = [r[0] for r in con.execute(
        "SELECT DISTINCT provider FROM external_player_week_usage WHERE season = ? ORDER BY 1", [season]).fetchall()]
    if not providers:
        return {"provider": None, "status": "none", "providers": [], "message": "no external usage for this season"}
    if len(providers) == 1:
        return {"provider": providers[0], "status": "single", "providers": providers,
                "message": f"using the only provider: {providers[0]}"}
    if preferred and preferred in providers:
        return {"provider": preferred, "status": "preferred", "providers": providers,
                "message": f"multiple providers; using configured preference {preferred}"}
    return {"provider": None, "status": "conflict_unresolved", "providers": providers,
            "message": f"multiple providers {providers} and no matching EXTERNAL_USAGE_PREFERRED_PROVIDER"
                       f"{f' (configured: {preferred})' if preferred else ''}; external usage NOT applied"}
