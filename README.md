# fantasy

Fantasy football analytics. The application lives in [`fantasy-intel/`](fantasy-intel/):
a FastAPI + DuckDB backend (`fantasy-intel/backend`) and a Next.js dashboard
(`fantasy-intel/frontend`).

```bash
git clone <this repo> fantasy
cd fantasy/fantasy-intel
cp .env.example .env              # league settings; see fantasy-intel/README.md

cd backend && uv sync && uv run pytest            # backend
cd ../frontend && npm ci && npm test              # frontend
```

Full setup, data sources, sync scripts and CI notes: [`fantasy-intel/README.md`](fantasy-intel/README.md).
