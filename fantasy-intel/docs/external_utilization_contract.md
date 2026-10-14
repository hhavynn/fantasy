# External utilization contract (routes & situational usage)

Provider-neutral ingestion for player-week utilization that nflverse doesn't
publish in-season: routes, route participation, alignment, pass-block snaps and
situational route shares. Code lives in `backend/app/external/`; the CLI is
`scripts/import_external_usage.py`.

```
authorized/manual normalized CSV
        │  parse + validate (contract.py)       hard error → whole file rejected
        ▼
provider-neutral rows ── identity (identity.py) ── unmatched/ambiguous kept, gsis_id NULL
        │
        ▼
DuckDB: external_usage_imports · external_player_week_usage · …_revisions · external_player_map · external_usage_issues
        │  provider selection (selection.py)
        ▼
player_week_usage  (LEFT JOIN; nflverse stays canonical)  →  player page / data-quality page
```

## 1. Why routes are external

nflverse's participation data (routes, alignment, personnel) is released only
after the season (see `nfl_metrics_verified_contract.md` §9). Route-level data
during the season comes from commercial charting providers. This repo is public,
so it holds **only the generic adapter**. Provider data, credentials and
provider-specific clients stay out of it (§9 below).

## 2. Meaning vs source

Downstream code asks for `routes`, `route_participation`, `tprr`… and never for
`<vendor>.routes`. Provider identity is provenance only: `provider`, `dataset`,
`import_id`, `source_updated_at` and file hash. In `player_week_usage` it appears
only as `route_data_provider`, `route_data_dataset` and `route_data_updated_at`.

## 3. Normalized CSV format

UTF-8 CSV with a header row. Column order doesn't matter. Blank, `NA`, `N/A`,
`null`, `none`, `nan` and `-` all mean **not provided (NULL)**; an explicit `0`
is a measured zero.

### Minimum required

**Each file contains exactly one provider, one dataset and one season.** A file
that mixes any of them is rejected. Split multi-season exports into one file per season.

| field | rule |
|---|---|
| `season` | integer (e.g. 2026); **one distinct season per file** |
| `week` | integer 1..22 |
| `provider` | column **or** `--provider`; one per file |
| `dataset` | column **or** `--dataset`; one per file |
| identity | at least one of: `provider_player_id`; `gsis_id`; **or** `player_name` + `team` + `position` |

### Optional identity / provenance

`provider_player_id`, `gsis_id`, `player_name`, `team`, `position`,
`source_updated_at` (ISO-8601). All persisted timestamps are **UTC-naive**: a value
with an explicit offset or a trailing `Z` is converted to UTC and the offset dropped;
a value without an offset is taken as already UTC. This never depends on the
machine's timezone. Example: `2026-10-07T09:00:00-04:00` is stored as `2026-10-07 13:00:00`,
the same as `2026-10-07T13:00:00Z`, `…+00:00`, `2026-10-07T18:30:00+05:30` and the
naive `2026-10-07T13:00:00`.

### Optional metrics (all nullable)

| column | type / bound | semantics |
|---|---|---|
| `routes` | integer ≥ 0 | standard |
| `pass_snaps`, `pass_block_snaps` | integer ≥ 0; `pass_block_snaps <= pass_snaps` | standard |
| `route_participation` | fraction 0..1 | **provider-defined** (denominator varies) |
| `pass_block_rate` | fraction 0..1 | provider-defined |
| `slot_rate`, `wide_rate`, `inline_rate`, `backfield_rate` | fraction 0..1 | provider-defined (alignment charting) |
| `two_minute_route_share`, `long_down_route_share`, `short_down_route_share` | fraction 0..1 | **provider-defined situations** |
| `source_tprr`, `man_tprr`, `zone_tprr` | float ≥ 0, no upper bound | provider-defined |
| `source_yprr` | float (can be negative) | provider-defined |

Rates are fractions: `0.85`, not `85`. A value outside 0..1 is rejected so a
percent column can't slip in. TPRR has **no upper bound** because providers'
target and route definitions differ.

Columns for canonical nflverse stats (`targets`, `receptions`,
`receiving_yards`, `air_yards`, `target_share`, `fantasy_points`, …) are
**ignored with a warning**; they are never stored. Other unknown columns are
ignored with a warning.

### Synthetic example (fake players, fake numbers)

```csv
season,week,provider,dataset,provider_player_id,gsis_id,player_name,team,position,routes,route_participation,pass_snaps,pass_block_snaps,slot_rate,wide_rate,two_minute_route_share,source_tprr,source_updated_at
2026,5,example_provider,weekly_routes,EX-001,,Avery Example,KC,WR,34,0.92,37,0,0.31,0.66,1.00,0.24,2026-10-07T09:00:00
2026,5,example_provider,weekly_routes,EX-002,,Jordan Sample,KC,TE,22,0.59,37,9,0.40,0.10,,0.18,2026-10-07T09:00:00
2026,5,example_provider,weekly_routes,EX-003,,Casey Placeholder,BUF,RB,0,0.00,30,4,,,,,2026-10-07T09:00:00
```

(`Casey Placeholder` ran zero routes, so their TPRR/YPRR are NULL, not 0.)

## 4. NULL vs zero, and TPRR / YPRR

- Missing means **unknown / not provided**, never 0.
- `routes == 0` → `tprr` and `yprr` are **NULL** (the ratios are undefined).
  If a file sends `source_tprr`, `source_yprr`, `man_tprr` or `zone_tprr` with
  `routes = 0`, they are stored as NULL with a warning.
- **nflverse stays canonical** for targets, receptions, yards, air yards, target
  share and fantasy points. App-level ratios are:

  ```
  tprr = nflverse targets         / external routes   (routes > 0)
  yprr = nflverse receiving_yards / external routes   (routes > 0)
  ```

  The provider's own TPRR/YPRR are kept as `source_tprr` / `source_yprr` and
  compared. A gap above tolerance (TPRR: > 0.03 and > 15%; YPRR: > 0.25 and
  > 15%) is a **warning** that the definitions differ. Nothing is overwritten.

## 5. Player identity

`source_player_key` (stable per provider): `id:<provider_player_id>` if present,
else `gsis:<gsis_id>`, else `nm:<sha1(normalized name|team|position)>`. Rows are
keyed by `(provider, dataset, season, week, source_player_key)`, so unmatched
rows survive and a corrected re-import replaces the right row. Keep identity
columns consistent between a file and its corrections.

Resolution priority (first decisive rule wins):

1. **`gsis_id` in the file** → must exist in nflverse players (`high`). An
   invalid id makes the row `unmatched` (`invalid_gsis_id`). It is **not**
   re-guessed by name.
2. **`external_player_map`** (reviewed, manual) → `provider + source_player_key → gsis_id` (`reviewed`).
3. **Exact normalized name + team (as of that week, from nflverse weekly
   rosters) + compatible position**, only if exactly one player matches
   (`medium`). Normalization lowercases and strips accents/punctuation but
   **keeps suffixes**, so "Marvin Harrison" ≠ "Marvin Harrison Jr.". Generic NFL
   team-code aliases are applied (LAR→LA, WSH→WAS, JAC→JAX, …).

Never guessed. Each case is kept with gsis_id NULL and a reason: `ambiguous`
(several identical names), `suffix_or_punctuation_mismatch`, `team_mismatch`
(stale team / trade), `no_exact_match` (including unknown provider ids), and
`insufficient identity`. Fix one with:

```bash
uv run python ../scripts/map_external_player.py --provider P --source-player-key id:EX-001 --gsis-id 00-00XXXXX
```

This writes a reviewed `external_player_map` row and re-resolves stored rows.

## 6. Validation

**Hard errors** (the whole file is `rejected`; nothing is written except the
import record and its issues):
negative routes / pass snaps / pass-block snaps / TPRR; `pass_block_snaps >
pass_snaps`; any rate outside 0..1; invalid season or week; non-numeric metric;
unparseable `source_updated_at`; missing provider, dataset or identity; duplicate
`(provider, dataset, season, week, source_player_key)` inside one file; more than
one provider or dataset in one file (`mixed_provider_or_dataset`); more than one
season in one file (`mixed_seasons`); empty file.

**Warnings** (import proceeds): missing gsis match; ambiguous identity; ignored
canonical/unknown columns; ratio supplied with zero routes; routes > pass snaps;
routes + pass blocks > pass snaps; two source rows mapping to one gsis/week;
suspicious correction (routes changed > 5 and > 30%); identity changed in a
correction; partial week coverage (fewer teams than played). At analytics time:
source vs derived TPRR/YPRR discrepancy; route participation vs routes ÷ team
dropbacks (> 15 pts); multiple providers without a preference; unmatched or
ambiguous totals. Everything is shown on `/data`.

## 7. Idempotency, corrections, history

- **Exact duplicate file** (same sha256 + provider + dataset already imported):
  recorded as an explicit `duplicate_noop` import pointing at the original;
  nothing else changes. *Decision:* record rather than silently skip, so a re-run
  is visible in the import history.
- **Corrected file** (different bytes): rows upsert by key. Superseded values go
  to `external_player_week_usage_revisions` (previous values, old and new
  `import_id`). Every import stays in `external_usage_imports`.
- A rejected file (including a mixed-season file) writes only its own `rejected`
  import record and its issues. It writes no normalized rows, and the CLI doesn't
  run analytics for it (exit code 1).

## 8. Provider-defined semantics and provider selection

Situations such as "two-minute", "long down-and-distance" and "short
down-and-distance", alignment buckets, pass-block rate and route participation's
denominator **differ by provider**. They're preserved exactly as delivered,
marked `provider-defined` in the UI, and never relabelled as universal metrics.
App-level preferences, for documentation only: two-minute = last 2:00 of either
half; long down = 2nd/3rd & 7+; short down = 3rd/4th & ≤ 2. A provider's values
are treated as compatible only once its definitions are confirmed to match.

Provider selection per season (`selection.py`), with **no brand ranking in this repo**:
one provider → used; several → `EXTERNAL_USAGE_PREFERRED_PROVIDER` if set and
present, otherwise **none** (external usage isn't applied) plus a data-quality
warning. Within the chosen provider, if two datasets or source rows disagree on a
metric for the same player-week, that metric is NULL.

## 9. Public repository and licensing constraints

- Never commit provider files, API responses, credentials, cookies, or bulk
  provider ID lists. `data/imports/`, `data/staging/`, `*.vendor.csv` and
  `*.vendor.json` are git-ignored (a test checks this). Put files under
  `data/imports/external/`.
- No scraping, private-endpoint reverse engineering, login automation or
  vendor-specific clients in this repo.
- Tests and docs use synthetic data only.

## 10. How a provider adapter plugs in later

An authorized adapter (in a **private** repo/module, or here only if its license
permits distribution) only has to **produce the normalized CSV above** and call:

```python
from app.external.importer import import_external_usage
import_external_usage(con, csv_path, provider="<name>", dataset="<dataset>")
```

then run `run_analytics`. Optionally it sets `EXTERNAL_USAGE_PREFERRED_PROVIDER`
and maintains reviewed `external_player_map` entries. Nothing downstream needs
provider-specific code.

## 11. Score isolation (current state)

External metrics are **ingested, normalized, validated and displayed only**.
They don't feed opportunity score, value score, opportunity tier, role trend,
market signal (BUY/SELL/WATCH), context labels, waiver score, league interest or
suggested drops. Tests assert identical scores, signals and labels with and
without external data. Calibration belongs to V2.

## 12. Usage

```bash
cd fantasy-intel/backend
uv run python ../scripts/import_external_usage.py ../data/imports/external/sample.csv \
  --provider test_provider --dataset weekly_routes
```

Prints the import summary and issues. For an imported file it then recomputes
analytics for that file's season, so routes appear in `player_week_usage` and on the
player page. A rejected file exits with code 1 and triggers no analytics.
`--no-analytics` skips the recompute.
