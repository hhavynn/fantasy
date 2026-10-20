"""Re-record the committed golden responses for the E2E fixture league (backend/tests/golden/e2e/).

    cd backend && uv run python ../scripts/record_golden.py

Run it only when a change to an endpoint's output is intended; the diff of tests/golden/e2e/ is the review of that
change. tests/test_golden_legacy.py and tests/test_golden_v2.py compare against these files.
"""
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))

from golden.golden import E2E_DIR, E2E_SEASON, build_e2e_db, record  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = build_e2e_db(Path(tmp) / "e2e.duckdb")
        os.environ.update(FANTASY_DB_PATH=str(db), ESPN_SEASON=E2E_SEASON, FANTASY_API_KEY="", MULTILEAGUE_ENABLED="")
        from fastapi.testclient import TestClient
        from app.main import app
        cs = record(TestClient(app, raise_server_exceptions=False), app, db, E2E_DIR)
    print(f"recorded {len(cs)} golden responses in {E2E_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
