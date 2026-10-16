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
from .api.matchup import router as matchup_router
from .api.projection_promotion import router as projection_promotion_router
from .api.projection_performance import router as projection_performance_router
from .api.weekly_recap import router as weekly_recap_router
from .api.research import router as research_router
from .api.player_compare import router as player_compare_router
from .api.rankings import router as rankings_router
from .api.leagues_v2 import router as leagues_v2_router
from .api.kicker_projections import router as kicker_projections_router

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
app.include_router(matchup_router)
app.include_router(projection_promotion_router)
app.include_router(projection_performance_router)
app.include_router(weekly_recap_router)
app.include_router(research_router)
app.include_router(player_compare_router)
app.include_router(rankings_router)
app.include_router(leagues_v2_router)
app.include_router(kicker_projections_router)


@app.get("/health")
def health():
    return {"ok": True}
