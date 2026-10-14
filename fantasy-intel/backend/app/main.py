"""FastAPI entrypoint:  cd backend && uv run uvicorn app.main:app --reload"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .api.manager_scouting import router as manager_scouting_router

app = FastAPI(title="fantasy-intel", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(manager_scouting_router)


@app.get("/health")
def health():
    return {"ok": True}
