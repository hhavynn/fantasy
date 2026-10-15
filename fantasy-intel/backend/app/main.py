"""FastAPI entrypoint:  cd backend && uv run uvicorn app.main:app --reload"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .api.manager_scouting import router as manager_scouting_router
from .api.trade_discovery import router as trade_discovery_router
from .api.luck_lab import router as luck_lab_router
from .api.schedule_intelligence import router as schedule_intelligence_router
from .api.playoff_race import router as playoff_race_router
from .api.team_value import router as team_value_router

app = FastAPI(title="fantasy-intel", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(manager_scouting_router)
app.include_router(trade_discovery_router)
app.include_router(luck_lab_router)
app.include_router(schedule_intelligence_router)
app.include_router(playoff_race_router)
app.include_router(team_value_router)


@app.get("/health")
def health():
    return {"ok": True}
