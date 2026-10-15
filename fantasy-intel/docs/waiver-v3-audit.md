# Waiver V3 audit — 2026-10-14

Audited origin/main before implementation. The supplied ten-phase request is the specification.

## Confirmed behavior

- `build_waivers` reads stored FREEAGENT/WAIVERS rows, requires a value or 0.5% ownership, sorts by value/ownership, then takes the first 300. Low-owned players without historical scores disappear even with forward projections or a new depth-chart role.
- Opportunity uses positional percentiles, trend and shared availability. Player values and BUY/HOLD/SELL semantics are owned by player_scores and will remain unchanged. xFP is PPR-based even in non-PPR leagues.
- Needs use actual lineup assignment and availability weights. This is useful, but the generic depth utility formula is not a sufficient transaction decision.
- `suggest_drop` runs unconditionally, chooses the lowest raw-value nonstarter/non-IR player, checks only fixed-slot holes, and never establishes capacity, positional caps or acquisition cost. Unknown player values become zero.
- A null drop is allowed through CLAIM thresholds; the page renders every null drop as “open spot.” Neither inference is valid.
- CLAIM/WAIT/IGNORE are blended-score thresholds (0.55 opportunity + 0.45 fit + scarcity). FREEAGENT status does not exempt a player from CLAIM language. High opportunity can overshadow weak actual improvement.
- Competition is an explicitly heuristic bucket from needs, add tendencies, activity and resources. It is not a success probability. Own waiver priority and FAAB do not drive acquisition advice; unknown FAAB spent is treated as zero in competitor profiles.
- K and D/ST use the same percentile/bench-fit path, with generic depth constants, without forward matchup/projection comparisons.
- Stored weekly ESPN projections, NFL schedules, game lines, raw rosterSettings/positionLimits and acquisitionSettings can support a waiver-only adapter. No new network calls or schema migration are necessary.
- Snapshot injury status has limited freshness guarantees; current-week NFL reports are already filtered. Preserve shared availability semantics and expose the limitation.

## Implementation boundary

New pure waiver modules own legality/drop choices, roster utility/resource decisions, streaming, and a batched stored-data adapter. Keep existing row fields; add V3 diagnostics in components JSON. Only the waiver endpoint region and waiver page change. No changes to protected trade modules, shared injury logic, player scoring, frontend API/types, or database schema.

Legality is conditional on known snapshot/settings: distinguish open capacity, required drop, no defensible drop, unknown settings, and known incompatibility. Never imply ESPN live acceptance, lock status or an undroppable-list membership that is not stored.

## Validation plan

Tests first for legal transitions, opportunity costs, resource distinctions, low-owned coverage, streaming, missing data and finished seasons; fixture sync → analytics → API. Run the full backend suite, frontend lint/build and deterministic 10/12/14-team benchmarks. Request Codex review on the pushed focused PR and address findings. Do not merge.
