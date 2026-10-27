# Matchup live-progress evidence boundary (#173, phase one)

This note records what today's read-only matchup contract proves and where it stops. It does **not** authorize a new provider poller or claim player-by-player kickoff/final coverage.

| Existing field | Origin | What it establishes | What it does not establish |
| --- | --- | --- | --- |
| `matchups.winner` | Last stored league snapshot | Final outcome when HOME/AWAY/TIE is stored | Precisely when a match ended |
| `matchups.home_points` / `away_points` | Last stored matchup score | Score at the last successful sync | A live score or a provider-confirmed game in progress |
| `matchup.status` | `analytics/matchup.py::matchup_status` | Final if winner exists; otherwise in_progress for positive recorded scores; upcoming/unknown elsewhere | NFL game-by-game kickoff, half-time, injury, or final status |
| `actual_lineup` | Stored lineup snapshots | Last synced manager-set roster and recorded points after matchup start | Per-player live clock, official final state or completed snap count |
| `matchup.provenance` | Existing league/projection signal envelope | Last captured/superseded status, age and partial input coverage when available | Freshness of an unexposed independent live feed |

## Shipped in this PR

The existing frontend `statusLabel` now says **“In progress at last sync”** rather than the timeless “In progress.” Existing `ProvenanceBadge` on Matchup already explains age/unknown age. This does not change score calculations, API status values, provider I/O or refresh cadence.

## Remaining requirements before #173 can close

1. Verify from provider documentation/fixtures whether either ESPN or Sleeper offers authoritative per-player NFL game state and its source timestamp, without scraping or exceeding read limits. If unavailable, show **unknown**, not an inferred status from current wall-clock time.
2. Distinguish historical/final weeks from the current week and prevent requests that would spawn a sync per browser tab. Only add refresh after establishing a shared, bounded, single-flight read path and provider approval.
3. Add synthetic pregame, halftime, final, postponed, bye, stale, empty and partial-score fixtures with explicit source times; never replace missing projected-remaining points with zero.
4. End-to-end verify light/dark and 320/390/768/1440 widths, spectator/private authorization, stale snapshot messaging and source evidence.
5. Run exact-head CI and independent review before shipping; no production verification is claimed.

No provider writes, unlicensed APIs, new polling, paid services or inferred percentages.
