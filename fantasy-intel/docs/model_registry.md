# Approved production model registry and the future promotion process

**Status: the approved production model is `ifp-2.0.0`** (promotion event of 2026-10-16, an
`owner_approval` by the repository owner recorded with `override: true`: it cites no prospective or
held-out report, and `evaluate_promotion` correctly reports the transition as not activatable on
evidence). `ifp-1.0.0` is retired (`superseded`), is `ifp-2.0.0`'s `rollback_to`, and is still read
by the projection switch for a week with no `ifp-2.0.0` run. Its artifact hash is still unpinned, so
`plan_rollback` refuses until it is pinned. The projection switch reads
`projection_source.MODEL_VERSION` (`ifp-2.0.0`); the hosted app never imports the registry.
The pre-promotion manifest is kept as `backend/tests/fixtures/model_registry_pre_ifp2.json`; the
sections below describe the gate as designed.

| File | Role |
|---|---|
| `backend/app/analytics/model_registry.json` | The reviewed approval manifest (committed) |
| `backend/app/analytics/model_registry.py` | Reader, integrity checks, dry-run promotion gate, rollback planner (research only, stdlib for reading) |
| `scripts/model_registry.py` | `status`, `evaluate --proposed`, `plan-rollback` dry runs |
| `backend/tests/test_model_registry.py` | Fail-closed checks, isolated approval and rollback fixtures, production-unchanged and isolation tests |

## Where independent projections reach consumers today

| Consumer | Reads | How |
|---|---|---|
| Start/Sit (`analytics/start_sit.py`, `GET /api/start-sit`) | ESPN by default; `ifp-1.0.0` only with `source=independent` | `projection_source.weekly` (filters `model_version = MODEL_VERSION` and the league's scoring fingerprint) |
| Matchup and Home (`analytics/matchup.py`, `GET /api/matchup`) | same | same |
| `GET /api/projections/independent` and `/data` | the latest stored run for the week | `independent_projection_runs`; only `scripts/backtest_independent_projections.py project` writes it, and only from the `ifp-1.0.0` artifact |
| Promotion tracker (`GET /api/projections/independent/promotion`) | runs of `MODEL_VERSION` under the league scoring | read-only progress, decides nothing |
| Trades, waivers, team value, playoff race, player scores, lineups | ESPN only | never import independent projections (`test_canonical_espn_systems_do_not_read_independent_projections`) |
| ifp-2.0.0 challenger, weekly ledger, research models | nothing consumer-facing | shadow only (`test_weekly_pipeline_never_imports_into_consumers`) |

So a production switch would touch three places, all in one reviewed change: `MODEL_VERSION` and
`APPROVED_ARTIFACT_SHA256` in `production_pin.py` (and the tested copy of the version in
`independent_projections.py`), the artifact that `project` loads, and the registry manifest. The registry alone changes none of them.

## Approving artifacts for the consumer view (pin and weekly registration)

The hosted app reads only the database, never the registry file or the model files, so artifact approval
has to be visible in the database. Every run records `independent_projection_runs.artifact_id`, the sha256 of
the artifact **file bytes** (`dict:` + digest when no file was used, which never matches anything below).
The consumer view and the evidence readers apply one rule per model version
(`production_forecasts.artifact_gate`, constants in the stdlib-only leaf `backend/app/analytics/production_pin.py`):

| Model | Rule | Today |
|---|---|---|
| `ifp-1.0.0` (fallback, rollback target) | one immutable file: runs must match `production_pin.APPROVED_ARTIFACT_SHA256`; `None` = unpinned, every run counts as before | `None` |
| `ifp-2.0.0` (production, retrained weekly) | each artifact is approved by a row in the **write-once `approved_artifacts` table**: the run's `artifact_id` must be registered for the model (and the target week, when recorded) | enforced |
| anything else | no rule | n/a |

The rule covers the projection switch (`projection_source.weekly`, so Start/Sit, Matchup and Home with
`source=independent`), the evidence readers (`production_forecasts.pregame_forecasts`, `eligible_run_ids`,
`latest_run`, and so the ESPN benchmark and the Start/Sit evaluation) and the promotion tracker for the model
it tracks (`ifp-1.0.0`, the frozen-criteria gate). Excluded runs carry the reason
`artifact not registered as approved` (ifp-2.0.0) or `artifact not the approved production artifact`
(ifp-1.0.0 pin). When no approved run exists for the week the switch falls back by its existing rules
(ifp-2.0.0, then ifp-1.0.0, then ESPN with `available: false` and a reason). Response shapes are unchanged,
except that `run.artifact_verified` appears when verification ran (`true`), or `false` for an unverified run
(below). `GET /api/projections/independent` is a research view of the latest stored run, says so in its notes,
and is not filtered.

**ifp-2.0.0: weekly registration.** `scripts/learning_cycle.py` trains the week's artifacts into the write-once,
content-addressed store (`weekly_learning.save_artifact`: `artifacts/ifp-2.0.0/s<season>w<week>-<artifact_id>.json`),
then its `project` stage runs `backtest_independent_projections.py project --register-artifact learning_cycle`.
That call registers the store file it uses (`weekly_learning.register_saved_artifact`) in `approved_artifacts`
and stamps the run with the same value: **the run's `artifact_id` is `sha256(file bytes)` of that store file,
and the registered `artifact_sha256` is the same digest**. (The `<artifact_id>` in the file name is a different
digest, of the artifact content without its timestamp; it is only used to check that the file is the store's own.)
Registration refuses a file that is not exactly the store file of that artifact, so a hand-written or edited
artifact cannot be registered by accident, and a `project` run without `--register-artifact` never registers
anything: its ifp-2.0.0 run is excluded until its artifact is registered. The table is create-only DDL in
`schema.sql` (no schema version bump), keyed `(model_version, artifact_sha256)`, written
`INSERT ... ON CONFLICT DO NOTHING` and never updated or deleted, and it travels with the database copy that
`publish_motherduck.py` publishes.

**Manual pin of an ifp-2.0.0 artifact** (an artifact the cycle did not register, for example after a restore):
save it through the weekly store, then from `backend/`:

```
uv run python ../scripts/backtest_independent_projections.py project --model ifp-2.0.0 --season S --week W \
    --register-artifact manual_pin
```

which registers the newest store artifact for that week (recorded with `source = manual_pin`) and stores the run.

**Fail-open window and the upgrade transition.** Enforcement for a model starts with its first registered artifact.
Until then, a database without `approved_artifacts` (an old local database or a hosted copy published before the
owner's laptop migrated) **or** whose table has no row for `ifp-2.0.0` yet (it was just created by the upgrade) is
read as before: ifp-2.0.0 runs count, and the response says so (`run.artifact_verified: false` and a note in
`reason`). This is deliberate: failing closed would blank the owner's "Ours" view until the next cycle. The first
learning cycle after the upgrade, and any `project --register-artifact` run, first registers the run's artifact
and then **backfills** every other artifact already in the local write-once weekly store
(`weekly_learning.backfill_store_artifacts`, source `learning_cycle_backfill`). Only the store's own
`s<season>w<week>-<artifact_id>.json` files qualify, and only if they load, belong to the model, pass the artifact
checks (schema, scoring, data cutoff before the target week, not a retrospective replay) and match their
content-addressed name; anything else is skipped and listed in the command's `artifact_backfill.skipped`, and
nothing outside the store is read. It is idempotent. So runs that were made from store artifacts keep being served
after the first cycle; a run whose artifact file is not in the store (hand-written, or deleted) stays excluded
once enforcement has started.

**ifp-1.0.0 single-sha pin.** The pin lives in two places that must agree, because the hosted app never reads
the registry:

| Place | Field | Today |
|---|---|---|
| `backend/app/analytics/model_registry.json` | `ifp-1.0.0` entry `artifact.sha256` | `null` |
| `backend/app/analytics/production_pin.py` | `APPROVED_ARTIFACT_SHA256` (stdlib-only, registry-free copy) | `None` |

`test_consumer_artifact_pin_is_a_tested_copy_of_the_registry_pin` fails CI if one is pinned without the other
(the same way the `MODEL_VERSION` copy is tested), and `production_pin.PINNED_MODEL_VERSION` /
`REGISTERED_MODEL_VERSION` are tested equal to `projection_source.FALLBACK_VERSION` / `MODEL_VERSION`.
`scripts/model_registry.py status` also reports `consumer_pin_matches`. To pin (a reviewed, owner-approved
change in one commit):

1. On the machine that holds the artifact, from the repo root:

   ```
   shasum -a 256 data/models/independent_projections_ifp-1.0.0.json
   ```

   (`sha256sum` on Linux). The registry script has no hashing command; its `status` output shows the digest it
   computed for the production entry's pinned file once one exists.
2. Put the 64-character lowercase hex digest in **both** `model_registry.json` (the `ifp-1.0.0` entry's
   `artifact.sha256`) and `APPROVED_ARTIFACT_SHA256` in `production_pin.py`.
3. `cd backend && uv run python ../scripts/model_registry.py status` must show `consumer_pin_matches: true`;
   run the backend tests. Fallback runs made by any other file stop being served.

A promotion or rollback (below) changes `MODEL_VERSION`, `FALLBACK_VERSION` and the `production_pin` constants
together.

## The manifest

Each model entry records:

| Field | Meaning |
|---|---|
| `family`, `version` | e.g. `ifp`, `ifp-1.0.0` |
| `state` | lifecycle state (below) |
| `artifact` | `{path, sha256}`: a path inside `INDEPENDENT_MODEL_DIR` and the sha256 of its **exact bytes**; `null` = unpinned (never trusted) |
| `scoring_fingerprint` | the league scoring the model was trained under |
| `training_cutoff` | `{season, week}` of the last week the artifact saw |
| `approval` | `incumbent`, `owner_approval` (`approved_by`, timezone-aware `approved_at`, `evidence_sha256`, `start_sit_ready`) or `rollback_restore` |
| `validation_reports` | `[{kind, path, sha256}]` with kind `prospective_shadow_evaluation`, `heldout_backtest` or `historical_research` |
| `rollback_to` | the version a promotion replaced, restorable by a rollback |
| `reason` | human-readable reason for the entry's state |

plus `production` (`{family, version}`, must name the single `approved_production` entry) and an
append-only `history` of `registry_initialized`, `promotion` and `rollback` events.

Lifecycle states:

| State | Meaning | Consumers |
|---|---|---|
| `experimental_research` | historical backtests only (e.g. `ifp-hgb-0.1.0`) | never |
| `prospective_shadow` | frozen pregame forecasts in the weekly ledger (`ifp-2.0.0`) | never |
| `eligible_for_review` | every evidence check passes; waiting for a human decision | never |
| `approved_production` | exactly one model, with an approval record | the opt-in switch, once wired |
| `deprecated_or_rolled_back` | retired; artifact, reports and ledgers kept | never |

Initial manifest: `ifp-1.0.0` is `approved_production` with an **`incumbent`** record (the model the
switch already read when the registry was created). That record is not an owner approval: it
authorizes nothing new, and `validate_manifest` refuses it on any model other than the registry's
initial production model. Its artifact hash, training cutoff and held-out report hash are **not
pinned yet** because the files are local and git-ignored; `status` reports this as the first action
item before any promotion.

## Promotion checks (fail closed)

`evaluate_promotion(committed, proposed)` judges a future promotion; `review_candidate` gives the
same evidence checks for a non-production model on the current manifest (`eligible_for_review`
at most, `activatable` always false). Every check is `{name, result, observed, required,
explanation}`; `insufficient_data` is never a pass, and `activates` is always `false`.

| Check | Passes only when |
|---|---|
| `transition` | `verify_transition`: both manifests valid, no model removed, no pinned artifact/report hash or scoring changed, history append-only, previous production retired, a final `promotion` event (from the outgoing model, `at`/`by` equal to the owner approval, a reason), or a final `rollback` event whose `rollback_restore` target is the outgoing model's `rollback_to`, was production before and has a pinned artifact |
| `artifact_integrity`, `artifact_loads` | pinned sha256, a relative path inside the model directory, the file exists and its bytes hash to the pin (a newer file next to it never counts); the bytes load with the family's production loader (`Artifact.from_dict`) with a well-formed model for every position and published intervals in the shape `interval_for` reads (finite bounds across the prediction range). Every manifest field is type-checked, and any unexpected input shape is a failed check, never a crash. All evidence is parsed as strict JSON: NaN or Infinity anywhere fails |
| `scoring_fingerprint` | manifest, artifact and live league agree, and the artifact names the registered version |
| `training_cutoff` | recorded |
| `prospective_shadow_evaluation_report` | exactly one, pinned and verified (a `historical_research` report never substitutes) |
| `prospective_report_subject`, `heldout_report_subject`, `artifact_report_binding` | the prospective report's `subject` names this candidate, the model it replaces and the scoring (`scripts/weekly_learning.py evaluate` derives it from the artifacts every counted pregame run recorded, `weekly_learning.report_subject`; a ledger mixing versions has no subject); the held-out report's `model_version` and `scoring_fingerprint` are the candidate's; the artifact records the sha256 of exactly that held-out report (`config.provenance.heldout_report_sha256`) and its embedded result (`fit_final` copies it) matches field by field. A strong report about another model never counts |
| `pregame_provenance` | the report's evidence kind is `pregame` (retrospective replays are never evidence) |
| `finalized_evaluation` | every counted week is distinct, fully final and not preliminary |
| `<POS>:*` | `weekly_learning.shadow_criteria`, **recomputed** from the report's metrics per position: >= 8 prospective weeks, >= 400 matched appearances, champion-relative MAE CI upper < 0, bias guard, starters-pool non-inferiority, 80% interval coverage 75-85% |
| `<POS>:production_baseline` | the held-out report meets `PROMOTION_CRITERIA` (n >= 400, >= 3% better than the best trailing baseline, CI upper < 0) |
| `<POS>:espn_rule` | required when the approval sets `start_sit_ready`: >= 300 matched pregame ESPN observations and MAE <= ESPN; otherwise `not_applicable` and the consumer keeps "not validated against ESPN" |
| `rollback_reference`, `rollback_artifact_integrity`, `rollback_scoring` | `rollback_to` is the model being replaced, whose artifact is pinned, present, and whose bytes are a JSON artifact naming that version under the league's scoring |
| `lifecycle_state`, `human_approval` | the new production entry is `approved_production` with an `owner_approval` naming who and when, citing the sha256 of the exact prospective and held-out reports verified above |

## Future promotion process (not executed)

1. Pin the model that is to be production (or, for a rollback, the restored model's file; a weekly-retrained
   production model is instead approved per artifact, see above): a reviewed
   change that fills its artifact sha256 (`shasum -a 256 data/models/independent_projections_ifp-1.0.0.json`
   for the `ifp-1.0.0` file), training cutoff and held-out report hash, and sets
   `production_pin.APPROVED_ARTIFACT_SHA256` and `PINNED_MODEL_VERSION` to match (see "Pinning the
   production artifact"). From then on the file can never be swapped silently, and the switch serves only
   production-model runs made by exactly that file.
2. Keep the challenger in the frozen pregame ledger until `scripts/weekly_learning.py evaluate`
   has >= 8 fully final prospective weeks; freeze that report (never edit it) and note its sha256.
3. Fit the candidate's single production artifact under the league scoring and copy it to a new,
   versioned path in the model directory (never over an existing file). Record its sha256 and cutoff.
4. `uv run python ../scripts/model_registry.py status` until the candidate is `eligible_for_review`.
5. Human review of the evidence. Only then write the proposed manifest: the candidate becomes
   `approved_production` with an `owner_approval` citing the report hashes, `rollback_to` the
   incumbent, the incumbent `deprecated_or_rolled_back`, a `promotion` history event.
6. `uv run python ../scripts/model_registry.py evaluate --proposed proposed.json` must report
   `activatable: true`.
7. A separate, explicitly authorized deployment PR applies the manifest and changes the consumer
   wiring listed above in the same commit, with the production-unchanged tests updated deliberately.

The registry cannot bypass a threshold: every gate is recomputed from verified evidence, and the
consumer wiring is a code change the registry never makes.

## Rollback design (planned, never applied here)

`plan_rollback(manifest, reason, by, at, ...)` (CLI `plan-rollback --reason --by --out`) returns the
manifest a rollback would produce, and writes it only to a **new** file (write-once, temp + fsync +
link; it refuses the committed path):

* the current production model becomes `deprecated_or_rolled_back` with `retired: {at, by, reason}`;
* its `rollback_to` model (which must be retired and previously production per `history`) becomes
  `approved_production` with a `rollback_restore` record keeping its previous approval;
* a `rollback` event `{at, event, from, production, by, reason}` is appended to `history`;
* it refuses when the target's artifact is unpinned, missing or changed, or was trained under a
  scoring the league no longer uses (incompatible scoring is detected, never restored);
* it is pure: no artifact, report or forecast ledger is touched (tested byte for byte), and
  `verify_transition` accepts the result (nothing removed, history only grows).

Applying it is the same reviewed change as a promotion: manifest plus consumer wiring.
Historical forecast ledgers stay immutable throughout; they record which model made each forecast.

## Known limitations

* `scripts/backtest_independent_projections.py fit` does not yet record `heldout_report_sha256` in the
  artifact (it was left unchanged so the ifp-1.0.0 artifact format is untouched). A future
  candidate's fit must record it, or the gate reports `artifact_heldout_report_sha` as
  `insufficient_data`.

* The participation artifact (`ipm-1.0.0`) that turns conditional points into expected points is not
  a separate registry entry; a promotion that changes it must pin it in the same reviewed change.
* `GET /api/projections/independent` shows the latest stored run of a week without filtering by
  model version. Today only `project` writes runs, from the `ifp-1.0.0` artifact; the deployment PR
  of a future promotion must add a registry-equivalent version filter there.
* ifp-2.0.0 retrains per target week, so it has no single sha in the manifest (`artifact.sha256` stays
  `null`); its artifacts are approved one by one in the database (`approved_artifacts`, see above). That
  table is a local-trust record, not an authenticated one: anyone who can write the live database can add a
  row. It guards against serving a run of an artifact nobody registered, not against a malicious local writer.
