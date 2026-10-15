Recorded-shape Sleeper API responses for offline tests. Structure, field names and types match
real `api.sleeper.app/v1` responses (checked 2026-10-14); names and ids are invented, and the
players file is a small subset of `/players/nfl`. `bracket_id` is deliberately a JSON number
above 2^53, as Sleeper sends it. `ff_playerids.csv` mimics the DynastyProcess ID map columns
(including its own team codes such as KCC / LVR) and is read with polars type inference so
`sleeper_id` arrives as an integer column, exactly like `nflreadpy.load_ff_playerids()`.
