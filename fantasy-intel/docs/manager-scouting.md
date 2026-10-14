# Manager scouting

`GET /api/managers/{team_id}/scouting` builds a read-only response on demand. The dedicated router uses the existing read-only connection dependency. No tables, migrations, schema versions, metric formulas or need formulas change. The existing manager detail endpoint stays intact; the UI fetches the two endpoints concurrently.

## Sources and interpretation

- Identity and counters: `fantasy_teams`, with observed metric counts shown separately. Counters may differ from visible activity coverage.
- Historical position and waiver behavior: `manager_metrics` and `manager_classifications`. Stored labels, confidence, minima, rules and league comparisons are surfaced without rerunning the metrics engine. Missing classifications are INSUFFICIENT.
- Position ratio: team add share divided by the weighted league add share (sum of positional add numerators / sum of known-position add denominators). Requires >=5 team adds, >=20 league adds, >=2 observed teams and a nonzero league positional share. Otherwise the ratio is null. This derived benchmark does not replace the engine's stored, team-relative classification.
- Current construction: exactly the league's current scoring-period `lineup_snapshots`, joined to `fantasy_players`. Every observed team's zero counts contribute to league averages; teams without a snapshot do not. No older snapshot is silently treated as current. IR players are included in their natural positions, with IR also shown as a subset. Unknown player positions are disclosed.
- Historical average roster size stays separate from current construction. Only the existing RB roster classification can generate a historical RB-roster headline. Current roster counts never become acquisition preferences.
- Streaming: stored distinct starters, changed/observed starter transitions and pickups. The existing metric engine compares successive observed snapshots, which can span a missing week. Pickups alone never generate a streaming headline.
- Needs: exact stored `team_position_needs` fields and details, including league starter average. The high display band is >=0.66 and is only wording for a stored current-state score; the formula is untouched. Starter values use the existing value-score scale, not new point projections.
- Trades: existing `load_trades` merges completed activity-feed legs and executed transaction legs; pending and canceled transactions are excluded. Source IDs are retained. Totals include all observed trades, while the UI shows the latest ten. ESPN can omit legs, so all counts describe visible evidence. Unequal player counts are facts, not demonstrated preferences.

## Confidence

Historical tendency confidence comes unchanged from the metrics engine and is distinct from HIGH/MEDIUM/LOW tendency labels. Missing history remains INSUFFICIENT. Before presenting a stored label as active, the report checks that the metric exists and agrees with its classification's value, sample size and computation timestamp (when both timestamps are available). Missing/mismatched evidence is marked explicitly and the active label/confidence are INSUFFICIENT; the original classification remains in `stored_classification` and the evidence disclosure. No formulas are rerun. Current roster headlines describe observed state, never historical preferences. Current need headlines use CURRENT_STATE to distinguish stored scores from inferred behavior.

Trade behavior confidence is based only on completed-trade sample size: **<5 INSUFFICIENT; 5–9 LOW; 10–19 MEDIUM; >=20 HIGH**. This deliberately conservative rule avoids inheriting the legacy trade-activity confidence based on elapsed weeks. It expresses only sample sufficiency. The report makes no trade-preference prediction at any sample size.

Every headline is deterministic and carries its own evidence and confidence. Section timestamps expose potentially different ages of snapshots, needs and metrics. Full classifications, raw metrics, weekly construction, roster, failed claims and transaction history remain available in expandable evidence sections.

## Verification

From `backend`, run `uv run pytest` and `uv run python ../scripts/check_manager_scouting.py --db /path/to/fantasy.duckdb`. The smoke script opens DuckDB read-only, checks every team, and identifies the user's team and move/trade extremes. It does not load or print auth values. From `frontend`, run `npm run lint` and `npm run build`.
