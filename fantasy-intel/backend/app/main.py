"""FastAPI entrypoint:  cd backend && uv run uvicorn app.main:app --reload"""

import hmac

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .api.routes import router
from .api.manager_scouting import router as manager_scouting_router
from .api.trade_discovery import router as trade_discovery_router
from .api.luck_lab import router as luck_lab_router
from .api.schedule_intelligence import router as schedule_intelligence_router
from .api.playoff_race import router as playoff_race_router
from .api.team_value import router as team_value_router
from .api.independent_projections import router as independent_projections_router
from .api.start_sit import router as start_sit_router
from .api.add_drop import router as add_drop_router
from .api.claim_plan import router as claim_plan_router
from .api.matchup import router as matchup_router
from .api.projection_promotion import router as projection_promotion_router
from .api.projection_performance import router as projection_performance_router
from .api.projection_movers import router as projection_movers_router
from .api.weekly_recap import router as weekly_recap_router
from .api.research import router as research_router
from .api.player_compare import router as player_compare_router
from .api.rankings import router as rankings_router
from .api.leagues_v2 import router as leagues_v2_router
from .api.connect import router as connect_router
from .api.kicker_projections import router as kicker_projections_router
from .api.dst_projections import router as dst_projections_router
from .api.injury_impact import router as injury_impact_router
from .api.power_rankings import router as power_rankings_router
from .api.projection_ranges import router as projection_ranges_router
from .api.league_rules import router as league_rules_router
from .api.transactions import router as transactions_router

app = FastAPI(title="fantasy-intel", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """When FANTASY_API_KEY is set (hosted mode), only the dashboard, which sends it, may call the API."""
    key = get_settings().api_key
    if key and request.url.path != "/health":
        sent = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(sent.encode(), key.encode()):
            return JSONResponse({"detail": "Missing or wrong X-API-Key."}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def retire_classic_site(request: Request, call_next):
    """CLASSIC_SITE_ENABLED=0 (Phase 5, after the owner's sign-off): the single-league routes answer 404; every league
    is served under /v2/leagues/{lg}. /health and /v2 are unaffected."""
    if request.url.path.startswith("/api/") and not get_settings().classic_site_enabled:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def hide_v2_when_disabled(request: Request, call_next):
    """With MULTILEAGUE_ENABLED off, every /v2 path, method and trailing-slash variant is a plain 404,
    so a disabled deployment does not reveal (via 405s or redirects) that the routes exist."""
    path = request.url.path
    if (path == "/v2" or path.startswith("/v2/")) and not get_settings().multileague_enabled:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


app.include_router(router)
app.include_router(manager_scouting_router)
app.include_router(trade_discovery_router)
app.include_router(luck_lab_router)
app.include_router(schedule_intelligence_router)
app.include_router(playoff_race_router)
app.include_router(team_value_router)
app.include_router(independent_projections_router)
app.include_router(start_sit_router)
app.include_router(add_drop_router)
app.include_router(claim_plan_router)
app.include_router(matchup_router)
app.include_router(projection_promotion_router)
app.include_router(projection_performance_router)
app.include_router(projection_movers_router)
app.include_router(weekly_recap_router)
app.include_router(research_router)
app.include_router(player_compare_router)
app.include_router(rankings_router)
app.include_router(leagues_v2_router)
app.include_router(connect_router)
app.include_router(kicker_projections_router)
app.include_router(dst_projections_router)
app.include_router(injury_impact_router)
app.include_router(power_rankings_router)
app.include_router(projection_ranges_router)
app.include_router(league_rules_router)
app.include_router(transactions_router)


# The same routers again under /v2/leagues/{league_id}: authorized per league, served from that league's files
# (app/api/league_scope.py). With MULTILEAGUE_ENABLED off these answer 404 like every other /v2 path.
from .api.leagues_v2 import require_enabled  # noqa: E402
from .api.league_scope import CachedResponse, add_scope_headers, mount_legacy_routers, serve_cached  # noqa: E402

mount_legacy_routers(app, [
    router, manager_scouting_router, trade_discovery_router, luck_lab_router, schedule_intelligence_router,
    playoff_race_router, team_value_router, independent_projections_router, start_sit_router, matchup_router,
    projection_promotion_router, projection_performance_router, projection_movers_router, weekly_recap_router,
    research_router, player_compare_router, rankings_router, kicker_projections_router, dst_projections_router,
    injury_impact_router, power_rankings_router, projection_ranges_router, league_rules_router,
    transactions_router, add_drop_router, claim_plan_router,
], require_enabled, owner_only=[projection_promotion_router, projection_performance_router, research_router])
app.middleware("http")(add_scope_headers)
from .api.provider_copy import classic_copy  # noqa: E402
app.middleware("http")(classic_copy)
app.add_exception_handler(CachedResponse, serve_cached)
from .analytics.lineup import UNSUPPORTED_LINEUP, LineupTooLargeError  # noqa: E402


async def unsupported_lineup(request: Request, exc: LineupTooLargeError):
    """More flex slots than the exact lineup solver accepts (a provider can store any count): a fixed 422 on every
    route that does not answer it itself, instead of a 500."""
    return JSONResponse({"detail": UNSUPPORTED_LINEUP}, status_code=422)


app.add_exception_handler(LineupTooLargeError, unsupported_lineup)
# Outermost: Server-Timing on every response, the sampled perf log off by default (app/monitor/timing.py).
from .monitor.timing import ServerTimingMiddleware  # noqa: E402
app.add_middleware(ServerTimingMiddleware)


@app.get("/health")
def health():
    return {"ok": True}
