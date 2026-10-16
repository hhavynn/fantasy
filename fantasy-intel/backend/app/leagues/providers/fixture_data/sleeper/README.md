Recorded-shape Sleeper responses served by `app/leagues/providers/fixtures.py` when `SLEEPER_SOURCE=fixture`.
Names and ids are invented; structure and field names match the real `api.sleeper.app/v1` shapes
(`tests/fixtures/sleeper/README.md`). These files live under `app/` (not `tests/`) so the deployed app can
serve them; Vercel excludes `tests/**`. The files of league 1180000000000000001 plus state/players/ID map
must stay in step with `tests/fixtures/sleeper` (`tests/test_league_providers_fixtures.py` checks it).
`players.json` / `ff_playerids.csv` here additionally contain three different players named "Mike Williams"
(sleeper ids 7001 / 7002 resolved to different gsis ids, 7003 unresolved) for the no-matching-by-name tests.
League 1180000000000000002 is a redraft half-PPR league with a TE premium, FLEX only (no superflex),
rolling waivers.
