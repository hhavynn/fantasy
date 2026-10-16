# Approved production model registry and the future promotion process

**Status: preparation only. The approved production model is `ifp-1.0.0` and stays `ifp-1.0.0`.**
The registry promotes nothing, activates nothing and is read by research tooling only. Every
consumer value is unchanged: the projection switch keeps reading `projection_source.MODEL_VERSION`
(`ifp-1.0.0`), and the hosted app never imports the registry.

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

So a production switch would touch three places, all in one reviewed change: `MODEL_VERSION` in
`projection_source.py` (and its tested copy in `independent_projections.py`), the artifact that
`project` loads, and the registry manifest. The registry alone changes none of them.

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
| `artifact_integrity`, `artifact_loads` | pinned sha256, a relative path inside the model directory, the file exists and its bytes hash to the pin (a newer file next to it never counts); the bytes load with the family's production loader (`Artifact.from_dict`) with a well-formed model for every position. All evidence is parsed as strict JSON: NaN or Infinity anywhere fails |
| `scoring_fingerprint` | manifest, artifact and live league agree, and the artifact names the registered version |
| `training_cutoff` | recorded |
| `prospective_shadow_evaluation_report` | exactly one, pinned and verified (a `historical_research` report never substitutes) |
| `prospective_report_subject`, `heldout_report_subject`, `artifact_report_binding` | the prospective report's `subject` names this candidate, the model it replaces and the scoring (`scripts/weekly_learning.py evaluate` writes it); the held-out report's `model_version` and `scoring_fingerprint` are the candidate's; the artifact records the sha256 of exactly that held-out report (`config.provenance.heldout_report_sha256`) and its embedded result (`fit_final` copies it) matches field by field. A strong report about another model never counts |
| `pregame_provenance` | the report's evidence kind is `pregame` (retrospective replays are never evidence) |
| `finalized_evaluation` | every counted week is distinct, fully final and not preliminary |
| `<POS>:*` | `weekly_learning.shadow_criteria`, **recomputed** from the report's metrics per position: >= 8 prospective weeks, >= 400 matched appearances, champion-relative MAE CI upper < 0, bias guard, starters-pool non-inferiority, 80% interval coverage 75-85% |
| `<POS>:production_baseline` | the held-out report meets `PROMOTION_CRITERIA` (n >= 400, >= 3% better than the best trailing baseline, CI upper < 0) |
| `<POS>:espn_rule` | required when the approval sets `start_sit_ready`: >= 300 matched pregame ESPN observations and MAE <= ESPN; otherwise `not_applicable` and the consumer keeps "not validated against ESPN" |
| `rollback_reference`, `rollback_artifact_integrity`, `rollback_scoring` | `rollback_to` is the model being replaced, whose artifact is pinned, present, and whose bytes are a JSON artifact naming that version under the league's scoring |
| `lifecycle_state`, `human_approval` | the new production entry is `approved_production` with an `owner_approval` naming who and when, citing the sha256 of the exact prospective and held-out reports verified above |

## Future promotion process (not executed)

1. Pin the incumbent: a reviewed change that fills `ifp-1.0.0`'s artifact sha256 (`sha256sum
   data/models/independent_projections_ifp-1.0.0.json`), training cutoff and held-out report hash.
   Pinning changes no behaviour, and from then on the file can never be swapped silently.
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
* ifp-2.0.0 retrains per target week, so it has no single immutable artifact yet (unpinned in the
  manifest); a promotion has to name one.
