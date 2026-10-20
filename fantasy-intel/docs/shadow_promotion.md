# Promote when proven: the research-shadow promotion check

Every research candidate runs as a prospective shadow. It freezes a write-once pregame ledger each week from 2026 week 7
and scores it once the week is fully final against a gate that was registered before any prospective week existed.
`scripts/shadow_promotion_check.py` watches all of them, so a candidate that passes is noticed the week it passes.
Nobody has to remember to look.

## What runs, and when

The weekly learning cycle (`scripts/learning_cycle.py run`, Tue-Thu) runs `promotion_check` as its last stage, after
`ctx_shadow`, the `RESEARCH_SHADOWS` (`injury_return_shadow` among them), `dst_shadow` and `dist_ledger`. It is a research stage like the others: a failure
is a warning in the cycle report and never fails the cycle or touches the production run. Skip it with
`--no-promotion-check`.

The check reads each shadow's saved gate result. It never re-scores a ledger and never re-implements a gate:

| Candidate | Shadow script | Saved result | A pass means |
|---|---|---|---|
| ifp-ctx-p1 | ctx_shadow.py | `data/models/research/context/shadow/scores.json` | `meets_gate` |
| ifp-ctx-p2q | qb_scenario_shadow.py | `data/models/research/qb_scenario_shadow/prospective.json` | `meets_gate` |
| ipm-cal-0.1.0 | participation_cal_shadow.py | `data/models/research/participation_cal_shadow/scores.json` | `meets_gate` |
| dfp-r1 | dst_shadow.py | `data/models/dst/shadow/scores.json` | `passes` |
| ifpd-0.1.0 | distribution_ledger.py | `data/models/research/point_distributions/ledger/scores.json` | every applicable range `within_bounds` |
| ifp-iwh-0.1.1 | injury_workload_research.py `prospective` | `data/models/research/injury_workload/prospective_ifp-iwh-0.1.1.json` | `meets_gate` |
| ifp-fr-p1, ifp-dct-p1, ifp-awd-p1 | signal_shadows.py | `data/models/research/signal_shadows/scores.json` | `eligible_for_review` |
| ifp-irs-p1, ifp-irb-p1, ifp-irsb-p1 | injury_return_shadow.py | `data/models/research/injury_return_shadow/scores.json` | `passes` (one look after 2026 week 18); if both singles pass, only the frozen joint choice is ready (`ifp-irsb-p1` = both columns together) |

For each candidate and position the summary lists the weeks scored, the matched appearances, the shadow's own gate
status and checks, and the checks that fail. `ready_to_promote` is true only when all of these hold:

- the shadow's own gate status is its pass status;
- every one of its checks passes;
- at least 8 fully final weeks are scored (`MIN_WEEKS`, the repo's shadow-promotion minimum). Every gate already
  requires this except the distribution ledger, so the floor only adds a week count there.

A missing or unreadable result is `no_evidence`. Fewer than 8 weeks, or a gate that reports insufficient data, is
`accumulating`. A gate with enough data that does not pass is `fails_gate`. The earliest possible pass is around week
15.

## The signal

When a candidate is ready, the check writes:

- `PROMOTION READY: <candidate> <positions> (...)` to stderr. That lands in the cycle log
  (`data/models/weekly/logs/cycle-<stamp>.log`) and in the launchd stderr log. The cycle report also gets it under
  `promotion_ready`, with the full summary under `promotion_check`.
- The same line, appended to `data/logs/shadow-promotion.log`. A week with nothing ready logs a one-line count instead.
- An entry in `data/logs/promotion-queue.json`, with the positions, the weeks scored, the evidence files, the next
  `ifp-2.x` version, the first and last time it passed, and the `plan` command. Entries are never dropped. One that
  stops passing is marked `no_longer_ready`; one listed in `PROMOTED` is marked `promoted`.
- `data/logs/shadow-promotion-latest.json`, the full summary from the last run.

The scheduled job never edits production code, the model registry, a ledger or a database. Folding a candidate into
Ours is always a reviewed pull request.

## Folding a ready candidate into Ours (the one-command path)

```bash
cd fantasy-intel/backend
uv run python ../scripts/shadow_promotion_check.py plan <candidate>
```

`plan` prints the evidence, the positions that passed, the current production version and the next one (for example
ifp-2.1.0 to ifp-2.2.0), and the steps for that candidate's target. A session follows those steps. For an Ours
candidate:

1. Branch from the latest main.
2. Confirm the evidence with `check --dry-run` and read the evidence files.
3. Build the next `ifp-2.x` as the production spec plus the candidate's frozen change, on the passing positions only.
   Every other position keeps the production spec. Fit it on production's exact training rows with its frozen
   penalties, the way `app/analytics/injury_model.py` built ifp-2.1.0.
4. Register it the way ifp-2.1.0 was registered (`docs/model_registry.md`):
   - a `model_registry.json` entry with `approved_production`, citing the shadow evidence, plus a promotion event;
   - `projection_source.MODEL_VERSION` set to the new version and `FALLBACK_VERSION` set to the old one;
   - `production_pin.REGISTERED_MODEL_VERSIONS`;
   - `weekly_learning.train_production` and the learning cycle's `project` default.
5. Add the candidate to `PROMOTED` in `scripts/shadow_promotion_check.py`, for example
   `{"version": "ifp-2.2.0", "positions": ["RB", "WR"]}`. From then on its passing gate reads
   `PROMOTION CONFIRMED`, not `PROMOTION READY`.
6. Run the tests: a new test file for the model, version strings updated in the existing suites, and the full backend
   suite. Then the frontend fixtures that carry the version, lint, typecheck, unit tests and E2E.
7. Open the PR, wait for the Codex review, fix its findings, and hand it to the merge coordinator. Train and project the
   new version on the real data only after the PR merges, and take backups first.

D/ST (`dfp-r1`) and point ranges (`ifpd-0.1.0`) are not part of Ours points. Their `plan` steps point at the D/ST
production model and at the product's point ranges instead. QB ranges stay restricted.

## Candidates promoted ahead of their gate

When a candidate is folded into production before its shadow passes, as with the forced ifp-2.2 promotion, add it to
`PROMOTED` in the same pull request. Its shadow keeps scoring. When the gate passes, the check reports
`PROMOTION CONFIRMED: <candidate> <positions> (already in <version>)`, and the forced promotion is confirmed on
prospective evidence. Until then the summary shows it as `accumulating` or `fails_gate`, like any other candidate. If its
gate fails, the check writes `PROMOTION REVIEW: <candidate> <positions> fails its gate but is in <version>`, the signal
to review the forced promotion.

## Commands

```bash
uv run python ../scripts/shadow_promotion_check.py check             # summary, log line, queue (data/logs)
uv run python ../scripts/shadow_promotion_check.py check --dry-run   # print only
uv run python ../scripts/shadow_promotion_check.py queue             # the promotion queue
uv run python ../scripts/shadow_promotion_check.py plan ifp-fr-p1    # the promotion brief
```

`--ifp-dir`, `--dst-dir`, `--dist-dir` and `--logs-dir` override the default locations: `IFP_MODEL_DIR`,
`DST_RESEARCH_DIR` and `SHADOW_PROMOTION_LOG_DIR`, which default to `data/models`, `data/models/dst` and `data/logs`.
