"""Record a reviewed provider-player -> gsis_id mapping and re-resolve stored rows.

    cd backend && uv run python ../scripts/map_external_player.py \
        --provider test_provider --source-player-key id:P123 --gsis-id 00-0012345

Find unmatched/ambiguous source keys on the /data page (External utilization).
"""

import argparse
import sys

import _bootstrap  # noqa: F401

from app.analytics.pipeline import run_analytics
from app.config import get_settings
from app.db.connection import open_db
from app.external.importer import add_reviewed_mapping


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--source-player-key", required=True, help="e.g. id:<provider id> or nm:<hash> from /data")
    ap.add_argument("--gsis-id", required=True)
    ap.add_argument("--note")
    args = ap.parse_args()
    settings = get_settings()
    with open_db(settings.db_path) as con:
        n = add_reviewed_mapping(con, args.provider, args.source_player_key, args.gsis_id, args.note)
        print(f"mapped {args.source_player_key} -> {args.gsis_id}; re-resolved {n} stored rows")
        run_analytics(con, settings.nfl_season)
    return 0


if __name__ == "__main__":
    sys.exit(main())
