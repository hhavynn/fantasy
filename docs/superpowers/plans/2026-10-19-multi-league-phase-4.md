# Multi-league Phase 4: scoring-aware Ours + Sleeper live — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every league gets Ours, K and D/ST projections in its own scoring, Los Subies byte-for-byte as today; Sleeper leagues sync through the job queue into league files and every page renders for them.

**Architecture:** Ours, the kicker model and the D/ST model keep training on the laptop in the reference scoring (Los Subies). Their sealed runs travel in the global file as `ref_*` copies. Every league build (the sync job) turns them into the league's own runs: a verbatim copy when the league's fingerprint is the reference's, otherwise a re-scored copy (skill players: the Ours points scaled by the ratio of the player's recent stat mix scored both ways; K and D/ST: their stored expected statistics scored with the league's rules, which is exact because both scorings are linear in those statistics). Sleeper leagues reuse the existing `app/sleeper/` normalizer and the DynastyProcess id map into the same league tables, with projections from the league's own Ours runs through the projection-source seam.

**Tech Stack:** FastAPI, DuckDB, pytest, GitHub Actions (the Phase 3 drain), Next.js.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md) (Phase 4). Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md) §3.5 ("Ours re-scored to league scoring").

## Global Constraints

- Free tiers only.
- No engine rewrites. Engines change only to (a) receive the perspective team, (b) read through the request's connection, and (Phase 4 scope) (c) read projections through the existing projection-source seam.
- Los Subies golden-response tests pass at the end of the phase, on the fixture and on real Los Subies produced by the job path.
- Flags default off; with every new flag off production behaves as Phase 3 left it. Rollback: `SLEEPER_SOURCE=disabled`.
- Ambiguous provider errors never revoke credentials or delete league data.
- GitHub Actions logs are public: ids, durations and codes only.
- Sealed runs stay sealed: a run copied or re-scored into a league file carries a `content_sha256` that the existing `integrity_problem` readers verify, and an `artifact_id` the existing artifact gate accepts. Nothing labels re-scored numbers as trained in the league's scoring: the run's `evaluation.rescored_from` says what it was derived from and how.

## Status before this phase

- Phase 3 merged (PRs #153, #154, #155). Live: Los Subies syncs by jobs with Havyn's credential; a public ESPN league (`lg_5776…`, imported by Havyn on 2026-10-21) published through the dispatch path. Its first sync took 19.7 min, against a 5-minute target. Cause: the waiver engine's exhaustive flex-lineup search on a 3 FLEX + OP roster (1312 of 1316 s). Fixed separately in `h/fix-lineup-search`: the same answers, 29 s for that league.
- Deferred Phase 3 live check: invite → viewer access on production. It is covered by `test_connect_api.py::test_invites_are_created_by_admins_redeemed_once_and_revocable` and the authorization matrix, and is run by hand when a second account exists.
- Sleeper's API terms: Havyn's decision on 2026-10-21 was to build on it (free, read-only, attribution, no resale).

## Decisions taken in this plan

| Topic | Decision | Why |
|---|---|---|
| Two PRs | **4a** (Tasks 1–6): reference runs in the global file; scoring-aware Ours, K and D/ST for every league built by jobs; Los Subies published only by jobs. **4b** (Tasks 7–12): Sleeper sync into league files; provider-neutral projection default; engines through the seam; Sleeper pages; leak tests | Each PR is reviewable on its own. 4a changes nothing a user sees for Los Subies and gives ESPN leagues with other scoring their own Ours |
| How Ours reaches other scorings | **Re-score, don't retrain.** Ours (`ifp-2.2.0`) learns a points target in the reference scoring; its inputs (`xfpl`, expected points) are themselves in that scoring, so its points cannot be split into per-stat forecasts without a new model. Each skill row is scaled by `f = mix_points(target) / mix_points(reference)`, where `mix` is the player's average box score over his last 8 regular-season games (current and previous season, from the global `nfl_weekly_stats`), shrunk to his position's average mix when he has fewer than 3. The same `f` scales the interval and the explanation's contributions. When `mix_points(reference) < 1.0`: additive shift `points + mix_points(target) − mix_points(reference)`, never below 0 | Exact for the reference scoring (`f` is the same expression divided by itself = 1.0; the code also short-circuits to a verbatim copy). It keeps Ours's level (injury, role, depth) and changes only what the scoring rewards. Retraining per scoring would multiply the laptop's training time by the number of distinct scorings |
| K and D/ST | Their rows gain `stat_line.scoring_stats`: the expected value of every statistic their scoring resolver weights (K: made/missed/blocked FGs per distance bucket and PATs; D/ST: the `stats` vector `dst_research.project_season` already scores). Re-scored points = the league's scoring applied to `scoring_stats`, exact because both scorings are linear in them (a test checks equality with the model's own points for 4 scorings). Runs stored before this change have no `scoring_stats`, so they are copied only into leagues with the reference fingerprint | No model change, exact numbers |
| Where reference runs live | Global file tables `ref_independent_projection_runs/rows`, `ref_kicker_projection_runs/rows`, `ref_dst_projection_runs/rows`, `ref_projection_range_quantiles`, `ref_projection_performance_reports`, `ref_model_replays`, `ref_league_scoring(season, scoring_items)`: copies made by `split_database` from the laptop database. Every league file gets its own `independent_projection_*` / `kicker_*` / `dst_*` (league tables, as today) from them at build time | Model outputs in one scoring are league-independent facts (no rosters, teams or members: checked by a column allowlist test). League files keep the tables engines read, so no engine changes |
| Research tables | Copied into a league file only when its skill fingerprint equals the reference's | They are evaluations of Ours under the reference scoring; another scoring has no evaluation yet |
| Los Subies publisher | The sync job only. `refresh_all.py` stops running `publish_leagues.py`; a new `publish_global.py` publishes only the global file after the laptop's pipeline. `sync_espn.py` stays on the laptop as Ours's local input (league scoring, ESPN designation fallback) until Phase 7 moves Ours to Actions | One publisher for Los Subies. The laptop no longer needs to hold the league's pointer. The master plan's "launchd ESPN step removed" is the step that published Los Subies; the local ESPN pull is an Ours input |
| When jobs rebuild Ours | Every build re-derives the league's Ours, K and D/ST tables from the global file's references (they are not carried forward). The global file advances after each laptop pipeline run, and the next scheduled sync of each league picks it up | Same data on every path. `recompute` fan-out stays Phase 7 |
| Sleeper ids (4b) | Sleeper player ids map to ESPN ids through the DynastyProcess map already in the global file (`nfl_ff_playerids`: `sleeper_id`, `espn_id`, `gsis_id`). Unmapped players get a synthetic negative id below −10 000 000 (never an ESPN id); D/ST maps to ESPN's `-16000 - proTeamId` | Every league table and engine keys on `espn_player_id`. Mapping keeps them unchanged and joins Sleeper players to nflverse, Ours and photos |
| Sleeper scoring (4b) | Sleeper `scoring_settings` are translated into ESPN-shaped `scoring_items` (`{statId, points}`), stored in `leagues.scoring_items`; position bonuses (`bonus_rec_te`, `bonus_rec_rb`, `bonus_rec_wr`) become `{statId: 53, points, positionPoints: {"TE": x}}`, which `league_scoring` learns to read (ESPN never sends `positionPoints`, so ESPN fingerprints do not change). Unknown keys are listed as unsupported, never dropped | The resolvers, fingerprints and every consumer stay as they are |
| Projection default (4b) | A league's default `source` is `espn` when it has ESPN projections and `independent` otherwise (Sleeper). Pages say which. The seam's provider baseline is empty for Sleeper | No ESPN-shaped stand-ins: a Sleeper page never shows "ESPN proj" |

## File structure

| File | Responsibility |
|---|---|
| `backend/app/db/league_split.py` | `REFERENCE_TABLES` (source → `ref_` name); `split_database` writes the `ref_` copies and `ref_league_scoring` into the global file |
| `backend/app/analytics/rescore.py` (new) | `stat_mix`, `rescore_skill_rows`, `rescore_kicker_rows`, `rescore_dst_rows`, `apply_reference(con, season)`: builds the league's runs from the global `ref_` tables |
| `backend/app/analytics/kicker_live.py`, `scripts/dst_projections.py` | Add `stat_line.scoring_stats` to new rows |
| `backend/app/analytics/kicker_scoring.py`, `dst_research.py` | `expected_stats_points(scoring, scoring_stats)` helpers (pure) |
| `backend/app/sync/build.py` | Ours/K/D/ST tables are not carried forward; `apply_reference` runs before `run_analytics` |
| `scripts/publish_global.py` (new), `scripts/refresh_all.py` | Global-only publish; drop `publish_leagues.py` from the chain |
| `backend/tests/test_rescore.py`, `test_league_split.py`, `test_sync_build.py` | Tests below |
| 4b: `backend/app/sleeper/write.py`, `app/sync/sleeper_source.py`, `app/sync/worker.py`, `app/analytics/projection_source.py`, engines, frontend | Task 7+ |

---

## PR 4a

### Task 1: Reference copies in the global file

**Files:** Modify `backend/app/db/league_split.py`. Test `backend/tests/test_league_split.py`.

**Interfaces:** Produces `REFERENCE_TABLES: dict[str, str]` (source table → global name), `ref_league_scoring(season INTEGER, scoring_items JSON)` in every global file written from a database that has these tables. Consumes nothing new.

- [ ] **Step 1: failing tests**

```python
def test_global_file_carries_reference_copies_of_the_model_runs(tmp_path, e2e_db):
    out = split_database(e2e_db, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-21T00:00:00")
    con = duckdb.connect(str(out.global_path), read_only=True)
    src = duckdb.connect(str(e2e_db), read_only=True)
    for source, ref in REFERENCE_TABLES.items():
        assert con.execute(f"SELECT count(*) FROM {ref}").fetchone() == src.execute(f"SELECT count(*) FROM {source}").fetchone()
    got = con.execute("SELECT season, scoring_items FROM ref_league_scoring").fetchone()
    want = src.execute("SELECT season, scoring_items FROM leagues").fetchone()
    assert (got[0], json.loads(got[1])) == (want[0], json.loads(want[1]))


def test_reference_tables_hold_no_league_identity():
    """Columns that would identify a league, team or member never reach the global file."""
    forbidden = {"league_id", "team_id", "owner_id", "member_id", "swid", "team_name", "abbrev", "is_mine"}
    con = duckdb.connect(str(e2e_db_path()), read_only=True)
    for source in REFERENCE_TABLES:
        cols = {r[0] for r in con.execute(f"DESCRIBE {source}").fetchall()}
        assert not cols & forbidden, (source, cols & forbidden)
```

- [ ] **Step 2:** run `uv run pytest tests/test_league_split.py -q` → FAIL (`REFERENCE_TABLES` missing).
- [ ] **Step 3: implement.** In `league_split.py`:

```python
# Model outputs in the reference scoring (the laptop's league), shipped to every league build (Phase 4): the job
# copies or re-scores them into the league's own tables (app/analytics/rescore.py). League-independent: no rosters,
# teams or members (tests/test_league_split.py checks the columns).
REFERENCE_TABLES = {t: "ref_" + t for t in (
    "independent_projection_runs", "independent_projection_rows", "kicker_projection_runs", "kicker_projection_rows",
    "dst_projection_runs", "dst_projection_rows", "projection_range_quantiles", "projection_performance_reports",
    "model_replays")}
GLOBAL_TABLES = GLOBAL_TABLES | frozenset(REFERENCE_TABLES.values()) | {"ref_league_scoring"}
```

  and inside the `gl` branch of `split_database`, after the global tables are copied:

```python
            if alias == "gl":
                for source, ref in REFERENCE_TABLES.items():
                    if source in known:
                        con.execute(f"CREATE TABLE gl.main.{_ident(ref)} AS SELECT * FROM src.main.{_ident(source)}")
                con.execute("CREATE TABLE gl.main.ref_league_scoring AS SELECT season, scoring_items "
                            "FROM src.main.leagues")
```

  A source database that is itself a job's working copy (it contains `ref_*` tables copied from the global file) must not copy them again: `split_database` drops `ref_*` from `known` before classification and skips the copy when `ref_league_scoring` already exists in `src` (the job's split discards its global file anyway).
- [ ] **Step 4:** tests pass; `test_league_split.py` unclassified-table test still passes.
- [ ] **Step 5:** commit `League split: reference copies of the model runs in the global file`.

### Task 2: K and D/ST rows carry their expected statistics

**Files:** Modify `backend/app/analytics/kicker_model.py` (`expected_kicking`), `kicker_scoring.py`, `dst_research.py`, `scripts/dst_projections.py`. Test `backend/tests/test_rescore.py`.

**Interfaces:** Produces `stat_line["scoring_stats"]`:
- kicker: `{"fg": {"made"|"missed"|"blocked": {"0_39"|"40_49"|"50_59"|"60_plus": float}}, "pat": {"made"|"missed"|"blocked": float}}`;
- D/ST: `{str(statId): float}` (exactly the `stats` dict `project_season` scores).

Also produces `kicker_scoring.expected_points(scoring: KickerScoring, s: dict) -> float` and `dst_research.expected_points(scoring: DstScoring, s: dict) -> float`.

- [ ] **Step 1: failing tests.** For four kicker scorings (Los Subies from `tests/fixtures`, standard ESPN, a 5-point 50+ league and one that penalizes misses), `expected_points(scoring, line["scoring_stats"])` equals `expected_kicking(...)["points"]` to 1e-9 on the E2E fixture's kicker rows. For D/ST, `expected_points(scoring, stats) == scoring.points(stats)` for the same rows under three scorings.
- [ ] **Step 2:** FAIL (no `scoring_stats`).
- [ ] **Step 3: implement.** In `expected_kicking`, alongside `line`:

```python
    buckets = ("0_39", "40_49", "50_59", "60_plus")
    fg = {o: {b: round(lam_fg * float(np.sum(pd_[BUCKET_MASKS[b]] * p[BUCKET_MASKS[b]])), 9) for b in buckets}
          for o, p in (("made", pm), ("missed", pmiss), ("blocked", pb))}
    pat = {"made": round(lam_pat * pp, 9), "missed": round(lam_pat * max(1 - pp - pbp, 0), 9),
           "blocked": round(lam_pat * pbp, 9)}
    line["scoring_stats"] = {"fg": fg, "pat": pat}
```

  `kicker_scoring.expected_points`:

```python
REPRESENTATIVE = {"0_39": 30, "40_49": 45, "50_59": 55, "60_plus": 62}   # any yard of the bucket scores the same


def expected_points(scoring: KickerScoring, s: dict) -> float:
    """League points of a kicker's expected statistics (stat_line.scoring_stats). Exact: every scoring this module
    supports is constant within these four buckets and linear in the counts."""
    fg = sum(n * scoring.fg_points(REPRESENTATIVE[b], o) for o, by in s["fg"].items() for b, n in by.items())
    return fg + sum(n * scoring.pat_points(o) for o, n in s["pat"].items())
```

  The "constant within the bucket" claim is asserted by a test: for every supported scoring in the tests, `fg_points(d, o)` is equal for all `d` in each bucket. In `project_season`, add `"scoring_stats": {str(k): v for k, v in stats.items()}` to each output row. `scripts/dst_projections.py` copies `f["scoring_stats"]` into `stat_line`. `dst_research.expected_points = lambda scoring, s: scoring.points({int(k): v for k, v in s.items()})`.
- [ ] **Step 4:** tests pass. The kicker and D/ST backtest tests are unchanged, and `content_sha256` changes only for newly written runs.
- [ ] **Step 5:** commit.

### Task 3: `app/analytics/rescore.py`

**Files:** Create `backend/app/analytics/rescore.py`. Test `backend/tests/test_rescore.py`.

**Interfaces:**
- `stat_mix(con, season: int, week: int) -> dict[str, dict[str, float]]`: gsis_id → per-game average of `league_scoring.COMPONENT_COLUMNS` over the player's last 8 REG games before `week` (current and previous season). Fewer than 3 games: the position average, among players with at least 3 games, replaces him. Keys are `__pos__QB` etc. for the position averages.
- `rescore_skill_rows(rows: list[dict], mix: dict, ref: SkillScoring, target: SkillScoring) -> list[dict]`
- `rescore_kicker_rows(rows, target: KickerScoring) -> list[dict] | None`: None when a projected row lacks `scoring_stats`.
- `rescore_dst_rows(rows, target: DstScoring) -> list[dict] | None`: same.
- `apply_reference(con, season: int) -> dict`: fills the league's `independent_projection_runs/rows`, `kicker_projection_*`, `dst_projection_*` and, on a fingerprint match, `projection_range_quantiles`, `projection_performance_reports` and `model_replays` from the `ref_` tables. It returns counts per family, such as `{"skill": {"copied": n, "rescored": m, "skipped": k}, ...}`.

Rules:
1. Target fingerprint equals the reference's: every run and row is copied verbatim (same run_id, same content_sha256).
2. Otherwise each run gets `run_id = f"{src}~{target_fp[-8:]}"`, `scoring_fingerprint = target_fp`, `created_at` = the source's, `evaluation = {**src_evaluation, "rescored_from": {"run_id": src, "scoring_fingerprint": ref_fp, "method": "stat-mix ratio" | "expected statistics"}}`, and a `content_sha256` from that store's own `content_sha256(meta, rows)`.
3. Rows that are not `projected` (bye, withheld, no estimate) copy unchanged.
4. A target the resolver marks unsupported gets no runs for that family. Consumers then show the provider's numbers, as today for unsupported scoring.

- [ ] **Step 1: failing tests**

```python
def test_reference_scoring_is_a_verbatim_copy(built_e2e):
    con = built_e2e      # working DB: E2E global + its own league; reference scoring == league scoring
    before = con.execute("SELECT * FROM ref_independent_projection_rows ORDER BY run_id, espn_player_id").fetchall()
    out = apply_reference(con, 2026)
    after = con.execute("SELECT * FROM independent_projection_rows ORDER BY run_id, espn_player_id").fetchall()
    assert after == before and out["skill"]["rescored"] == 0


def test_half_ppr_target_scales_each_projected_row_by_its_mix_ratio():
    ref = resolve_skill_scoring(PPR_ITEMS)
    half = resolve_skill_scoring(HALF_PPR_ITEMS)
    mix = {"g1": {"receptions": 6.0, "receiving_yards": 80.0, "receiving_tds": 0.5, **ZEROS}}
    row = {**ROW, "gsis_id": "g1", "projected_points": 17.0, "interval_low": 5.0, "interval_high": 30.0,
           "p_play": 0.9, "expected_points": 15.3, "coverage_status": "projected"}
    (out,) = rescore_skill_rows([row], mix, ref, half)
    f = (3 + 8 + 3) / (6 + 8 + 3)
    assert out["projected_points"] == round(17.0 * f, 2)
    assert (out["interval_low"], out["interval_high"]) == (round(5.0 * f, 2), round(30.0 * f, 2))
    assert out["expected_points"] == round(out["projected_points"] * 0.9, 2)


def test_tiny_reference_mix_falls_back_to_an_additive_shift_never_below_zero(): ...
def test_unsupported_target_writes_no_skill_runs(): ...
def test_rescored_runs_verify_through_the_consumer_readers(built_e2e_half_ppr):
    apply_reference(con, 2026)
    sel = projection_source.select_run(con, 2026, WEEK)
    assert sel["run"] is not None and sel["run"]["run_id"].endswith(fp[-8:])
    assert sel["run"]["artifact_check"] in ("verified", "unverified")
def test_kicker_rows_without_scoring_stats_are_not_rescored(): ...
def test_dst_rescore_equals_the_model_points_for_the_target_scoring(): ...
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3: implement.** The skill row transform:

```python
def _scale(v, f):
    return None if v is None else round(v * f, 2)


def rescore_skill_rows(rows, mix, ref, target):
    out = []
    for r in rows:
        if r["coverage_status"] != "projected" or r["projected_points"] is None:
            out.append(dict(r))
            continue
        m = mix.get(r["gsis_id"]) or mix.get("__pos__" + (r["position"] or ""))
        a = None if m is None else points(m, ref)
        b = None if m is None else points(m, target)
        if a is None or b is None:
            out.append({**r, "coverage_status": "no_estimate", "projected_points": None, "expected_points": None,
                        "flags": [*(_json(r["flags"]) or []), "no box-score mix to re-score from"]})
            continue
        if a >= 1.0:
            f = b / a
            new = {k: _scale(r[k], f) for k in ("projected_points", "interval_low", "interval_high")}
            expl = [{**e, "contribution": _scale(e.get("contribution"), f)} for e in (_json(r["explanation"]) or [])]
        else:
            d = b - a
            new = {k: None if r[k] is None else round(max(r[k] + d, 0.0), 2)
                   for k in ("projected_points", "interval_low", "interval_high")}
            expl = _json(r["explanation"])
        pts = new["projected_points"]
        out.append({**r, **new, "explanation": expl,
                    "expected_points": None if r["p_play"] is None else round(pts * r["p_play"], 2),
                    "flags": [*(_json(r["flags"]) or []), "re-scored to this league's scoring"]})
    return out
```

  `apply_reference` opens the `ref_` tables only when `ref_league_scoring` exists (an old global file → no-op, returns `{"skipped": "no reference"}`). It reads the league's scoring with the same `_scoring` helpers the consumers use (`projection_source._scoring`, `kicker_store.league_fingerprint`, `dst_store.league_fingerprint`), so the run it writes is the run they select. Inserts go through `connection.insert_rows`. The function deletes the league's own rows of these tables first: the build starts from a working copy, and carried-forward rows must never mix with the new ones.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit.

### Task 4: Job builds use the references; Ours tables stop carrying forward

**Files:** Modify `backend/app/sync/build.py`. Test `backend/tests/test_sync_build.py`.

- [ ] **Step 1: failing tests:**
  - `test_build_derives_ours_from_the_global_references` (a build without a previous file has `independent_projection_runs` equal to the global `ref_` runs);
  - `test_carried_forward_ours_rows_are_replaced` (a previous file with an extra fake run → the built file does not have it).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: implement.**

```python
# Derived from the global file's reference runs on every build (app/analytics/rescore.py), never carried forward.
DERIVED_TABLES = frozenset({"independent_projection_runs", "independent_projection_rows", "kicker_projection_runs",
                            "kicker_projection_rows", "dst_projection_runs", "dst_projection_rows",
                            "projection_range_quantiles", "projection_performance_reports", "model_replays"})
```

  In `_carry_forward`, iterate over `set(tables) & (LEAGUE_TABLES - DERIVED_TABLES)`. In `build_league_file`, call `rescore.apply_reference(con, season)` after `write_synced`, before `run_analytics`. Tables that `apply_reference` leaves empty (old global file) are created empty by `init_db`.
- [ ] **Step 4:** `uv run pytest tests/test_sync_build.py tests/test_sync_worker.py -q` passes.
- [ ] **Step 5:** commit.

### Task 5: The laptop publishes only the global file

**Files:** Create `scripts/publish_global.py`. Modify `scripts/refresh_all.py` and `scripts/learning_cycle.py` (publish the global after a successful cycle, when `FI_SYNC_DB_URL` and R2 are set). Test `backend/tests/test_refresh_all.py`.

- [ ] **Step 1: failing tests.** `refresh_all.plan()` with `LEAGUE_PUBLISH_ID`, `FI_SYNC_DB_URL` and R2 set lists `publish_global.py` and not `publish_leagues.py`. `publish_global.py` against a `DirWriter` writes one global object and updates `fi.global_snapshot` (pg_harness), and a second run with unchanged content uploads nothing.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: implement.** `publish_global.py`:
  1. `split_database(settings.db_path, tmp/"league-unused", tmp/"global", stamp=…, global_stamp=GLOBAL_STAMP)`;
  2. `publish_global(out.global_path, out.global_version, writer, con)`;
  3. print the version and whether it was reused.

  `publish_leagues.py` stays for manual use (rollback path), and its docstring says so.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit.

### Task 6: Prove 4a

- [ ] Run `scripts/golden_check.py --job-path` on real Los Subies: 52 compared, 52 identical, including the `source=independent` routes and the Ours, kicker and D/ST endpoints.
- [ ] Do a local build of the public league `lg_5776…` with the current global: it gets Ours runs whose fingerprint is its own scoring's, or no runs with the reason stated when its scoring is unsupported. Spot-check five players' re-scored points by hand against the mix ratio.
- [ ] Run the full backend suite (CI shards) and E2E. Open the PR, wait for Codex (one round), fix, merge on green CI.
- [ ] On production after merge:
  1. run `publish_global.py` once by hand;
  2. dispatch the drain;
  3. check that Los Subies' `/v2` Ours pages are identical to before (the API response diff ignores timestamps), and that the public league shows Ours for `source=independent`.
- [ ] Roll back: put `publish_leagues.py` back in the chain (one line) and the job keeps working on the old global file.

## PR 4b (tasks detailed when 4a is merged)

- **Task 7: Sleeper scoring.** `positionPoints` in `league_scoring` (+ kicker/D/ST translations), `app/sleeper/scoring.py`: Sleeper `scoring_settings` → ESPN-shaped items; unknown keys unsupported. Tests: the two fixture leagues' fingerprints; TE premium re-scores TE rows only.
- **Task 8: Sleeper league writer.**
  - `app/sleeper/write.py`: `write_sleeper(con, data, *, synced_at, my_user_id)` turns `normalize.build_rows` output into the league tables (`leagues`, `league_members`, `fantasy_teams`, `lineup_snapshots`, `matchups`, `transactions`, `fantasy_players`, `player_id_map`).
  - Player ids are mapped through `nfl_ff_playerids`; actual points come from Sleeper matchups (`players_points`). There are no provider projections.
  - Tests: a fixture Sleeper league file has every table the pages read, and no colliding ids with an ESPN league.
- **Task 9: worker.**
  - `app/sync/sleeper_source.py` (fetch with `SleeperClient`, 4 provider slots, 429 pauses the provider, probe = league exists), `build_league_file` for Sleeper payloads, and raw payloads for `recompute`.
  - `SLEEPER_SOURCE=disabled` makes the claim skip Sleeper and the API answer "temporarily unavailable".
  - Connect: `POST /v2/leagues/import` accepts `provider: "sleeper"`.
- **Task 10: projection default.**
  - `projection_source.default_source(con)` returns `espn` when the league has ESPN projections and `independent` otherwise.
  - Every consumer's `source` default reads it.
  - Waivers, trades, ROS and team value read weekly and rest-of-season projections through `projection_source.weekly` / the Ours horizon runs instead of `espn_player_weekly_points` directly, and only for leagues whose default is `independent`. ESPN leagues are unchanged (goldens).
- **Task 11: pages.**
  - Labels come from the response's `source` ("Ours" / "ESPN"), never hard-coded, and the Add League flow takes a Sleeper id.
  - Playwright runs a Sleeper fixture league (half-PPR, superflex, TE premium) through every page with no "ESPN" label, no empty panel and no console error.
- **Task 12: leak tests and acceptance.**
  - `test_two_leagues_with_colliding_ids_never_leak` gains a Sleeper league.
  - The authorization matrix includes a Sleeper league.
  - Live run: import a public Sleeper league on production and check that it publishes.
