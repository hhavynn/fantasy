"""Load the pinned point-distribution artifact into the database so the API can serve floor / ceiling ranges
(app.analytics.projection_ranges; docs/research/fantasy_point_distributions.md section 18).

    cd backend
    uv run python ../scripts/projection_ranges.py load      # copy the current pin's quantile table (once per pin)
    uv run python ../scripts/projection_ranges.py status    # what the database serves

The pin is the one the frozen pregame distribution ledger uses (scripts/distribution_ledger.py: the first freeze pins
the newest artifact automatically; a re-pin is explicit). The artifact is verified byte for byte against the pin before
anything is written. `load` appends one table of rows per pin to projection_range_quantiles and is a no-op once that
pin is loaded; refresh_all.py runs it before the MotherDuck publish, which carries the table to the hosted site.

Exit codes: 0 loaded or already loaded; 3 nothing to load yet (no pin: run the distribution ledger's freeze once an
artifact exists); 1 the pinned artifact failed verification.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

import distribution_ledger as dl  # noqa: E402

from app.analytics import projection_ranges as pr  # noqa: E402
from app.config import get_settings  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_NOTHING = 0, 1, 3


def cmd_load(args) -> int:
    pin = dl.current_pin(args)
    if pin is None:
        print(f"WARNING: no {pr.DISTRIBUTION_VERSION} distribution pin under {dl.ledger_dir(args)}: nothing to load "
              "(distribution_ledger.py freeze pins the artifact)", file=sys.stderr)
        return EXIT_NOTHING
    try:
        art = dl.load_pinned(args, pin)
    except ValueError as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return EXIT_FAILED
    from app.db.connection import open_db

    with open_db(args.live_db) as con:
        written = pr.store(con, art, pin)
    print(json.dumps({"pin_id": pin["pin_id"], "content_hash": art["content_hash"], "rows_written": written,
                      "already_loaded": written == 0}, indent=1))
    return EXIT_OK


def cmd_status(args) -> int:
    from app.db.connection import open_db

    with open_db(args.live_db, read_only=True) as con:
        tables = pr.load(con)
    print(json.dumps(None if tables is None else {k: v for k, v in tables.items() if k != "positions"}
                     | {"positions": {p: {"method": t["method"], "status": t["status"], "grid_rows": len(t["grid"])}
                                      for p, t in tables["positions"].items()}}, indent=1, default=str))
    return EXIT_OK


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["load", "status"])
    ap.add_argument("--ifp-dir", type=Path, default=dl._path("IFP_MODEL_DIR", "data/models"),
                    help="holds research/point_distributions/ (the artifacts and the ledger's pins)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    return {"load": cmd_load, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
