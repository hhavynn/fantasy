"""Write the demo league the /leagues/demo page shows (issue #167) from the recorded engine output of the synthetic
fixture league. No network, no database, no credentials: it reads the goldens in backend/tests/golden/e2e (the API's
answers for scripts/build_e2e_db.py's invented "Fixture League") and keeps an allowlist of fields, so nothing beyond
team names, invented player names and the engines' numbers reaches the page.

    python scripts/build_demo_league.py            # writes frontend/lib/fixtures/demo-league.synthetic.json

Rerun it after the goldens are re-recorded (scripts/record_golden.py) to keep the demo's numbers the engines' own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "backend" / "tests" / "golden" / "e2e"
OUT = ROOT / "frontend" / "lib" / "fixtures" / "demo-league.synthetic.json"


def body(name: str) -> dict:
    text = (GOLDEN / f"{name}.json").read_text(encoding="utf-8")
    payload = json.loads(text.split("\n", 1)[1] if text.startswith("//") else text)
    if payload.get("status") != 200:
        raise SystemExit(f"{name}: golden status {payload.get('status')}, expected 200")
    return payload["body"]


def player(p: dict) -> dict:
    return {"name": p["name"], "position": p["position"], "pro_team": p.get("pro_team"), "projection": p.get("projection")}


def build() -> dict:
    overview, start_sit, waivers = body("api_overview"), body("api_start_sit"), body("api_waivers")
    league = overview["league"]
    summary = start_sit["summary"]
    decision = next((d for d in start_sit["decisions"] if d.get("actionability") == "ACTIONABLE"), None)
    standings = sorted(overview["standings"], key=lambda t: (t.get("playoff_seed") or 99, t["team_id"]))
    claims = sorted(waivers["players"], key=lambda p: -(p.get("waiver_score") or 0))[:3]
    return {
        "source": "Recorded engine output for the synthetic fixture league (backend/tests/golden/e2e). "
                  "Every team, player and number is an invented test input.",
        "league": {"name": league["name"], "season": league["season"], "week": league["current_week"],
                   "teams": league["size"], "reception_points": league["reception_points"]},
        "team": start_sit["team"]["name"],
        "lineup": {
            "status": summary["status"],
            "projected_gain": summary["projected_gain"],
            "current_total": summary["actual_projected_total"],
            "recommended_total": summary["recommended_projected_total"],
            "start": [player(p) for p in (decision or {}).get("start_players", [])],
            "bench": [player(p) for p in (decision or {}).get("bench_players", [])],
        },
        "standings": [{"team": t["name"], "wins": t["wins"], "losses": t["losses"], "ties": t["ties"],
                       "points_for": t["points_for"], "points_against": t["points_against"],
                       "seed": t.get("playoff_seed"), "mine": bool(t.get("is_mine"))} for t in standings],
        "waivers": [{"name": p["full_name"], "position": p["position"], "pro_team": p.get("pro_team"),
                     "recommendation": p.get("recommendation"), "waiver_score": p.get("waiver_score"),
                     "reason": next((r for r in p.get("reasons") or [] if not r.startswith("Opportunity")), None)}
                    for p in claims],
    }


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
