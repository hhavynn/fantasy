# #253: Dual-position roster evidence in league compatibility

This is an additive read-only diagnostic, not a new lineup or rank engine. No player IDs, names, league IDs, provider raw payloads, or private tenant data are returned.

## User-visible behavior

The read-only league facts contain `active_dual_position_players`, a **count** of distinct currently rostered, non-IR/taxi players for whom the synced provider supplied a second **base position**. Flex, bench and IR eligible-slot IDs alone never make a player dual-eligible. Missing/malformed eligibility evidence does not count and never grants a second position.

Where at least one such player exists, its **aggregate count** appears as the `value` of finding `dual_position_rankings` in the existing league-scoped `rankings` feature dimension (`partial`) with finding `dual_position_rankings`: rankings and position filters still classify a player under their **primary** position, even though roster lineup engines can honor provider-listed secondary eligibility. The finding says to use the lineup or trade evaluator for actual lineup impact. This distinction is not an error in the exact assignment solver.

## Certification boundaries

- Reuse `lineup.parse_eligible_slots`; never infer a second position from a flex slot or a nickname.
- Evaluate against the league-file roster snapshot for the current scoring period, scoped to the same authorized league file as every other compatibility fact.
- Synthetic FX-17 tests cover a provider-listed WR/RB player, duplicated rows, flex-only, IR-reserved, unknown and malformed evidence.
- The numerical count does **not** certify league-wide scoring fidelity or replacement-level rank accuracy. G8, G10 and other unsupported/unknown scoring families remain separate.
- ESPN and Sleeper readers remain read-only. No provider requests, new storage, migrations, service cost, or identity-bearing global endpoints.

Source: `backend/app/analytics/league_compatibility.py`, tests: `backend/tests/test_league_compatibility.py`.
