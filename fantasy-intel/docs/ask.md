# Ask Fantasy Intel (`ask/v1`, issue #219)

`GET /api/ask?q=<question>[&team_id=N]` (and `/v2/leagues/{lg}/api/ask`) answers one supported question about the
league from an **existing** read-only analysis, asks a specific clarifying question, or abstains. No model is used or
needed: a deterministic grammar (`app/analytics/ask.py`) compiles the question, and the executor (`app/api/ask.py`)
calls one existing handler with typed arguments. No paid service, no new data source, no SQL or request path built
from the question, nothing written or logged.

## Flow

1. **Authorize.** Under `/v2` the router is mounted by `mount_legacy_routers`, so `league_scope` authorizes the
   caller before the handler runs: unknown, private-to-others and anonymous-on-private leagues are the same
   `404 {"detail": "League not found."}` whatever `q` holds, and no league file is downloaded for them.
2. **Bound.** `q` is at most 200 characters (422 above that, after authorization).
3. **Parse.** NFKC, casefold, unified dashes/quotes/≥/≤, collapsed whitespace, polite lead-ins and trailing
   punctuation dropped. Then an ordered allowlist of **anchored** templates: a template must match the whole
   question and every slot must validate, or the next one is tried. Leftover words are never ignored, so an added
   unsupported condition abstains instead of being answered without it.
4. **Resolve.** Players are matched against the league's own player pool by an exact folded name (accents, case and
   `.` `'` `-` ignored), or a single word against last names. Same-name players → a clarification listing each
   candidate with position, NFL team and fantasy team, and a re-ask that adds a qualifier: `Papa WR (WR, SF)`, or
   `Papa WR (id 304)` when position and team are also shared. Intents about one team's roster need a team in view
   (the saved team, or `?team_id=`); without one the card asks for a team. There is no fallback team.
5. **Execute.** One handler call (two for a screen over "the last N weeks": the first learns the latest week with
   data so the range is exact weeks, not "last N games"). Handler 404/422/503 become a clarification, an abstention
   with the handler's own reason, or `unavailable`.
6. **Card.** Every number is copied from the handler response, never recomputed; missing stays `null`.

## Intents

| Intent | Example | Handler | Link |
|---|---|---|---|
| `screen` | WRs with a target share above 25% over the last 3 games | `screener.screen` | `/screener?pos=&fg=&fm=&fo=&fv=&from=&to=&last=` |
| `screen_preset` | Workhorse backs | `screener.screen` (a registry preset) | `/screener?...` |
| `waivers_best` | Who is the best available RB? | `routes.waivers(view=BEST)` | `/waivers?view=BEST&pos=` |
| `start_sit` | Who should I start this week? | `start_sit.get_start_sit` | `/start-sit?week=` |
| `replacement` | Best replacement for Papa WR | Start/Sit `alternatives` at that starter's slot, else the decision that benches him | `/start-sit` |
| `start_choice` | Should I start Papa WR or Quebec WR? | Start/Sit `alternatives` (the slot's what-if delta) | `/start-sit` or `/compare` |
| `matchup` | Who am I playing this week? | `matchup.get_matchup` | `/matchup?week=` |
| `player` | How is Papa WR doing? | `routes.player_detail` | `/players?id=` |
| `ownership` | Is Waiver RB available? | `routes.player_detail` | `/players?id=` |
| `compare` | Compare Papa WR and Quebec WR | `player_compare.compare_players` | `/compare?a=&b=` |
| `trade_value` | What is Papa WR's trade value? | `trade_values.trade_values` | `/trades/values?player=` |
| `trade_values_top` | Top trade values at WR | `trade_values.trade_values` | `/trades/values?pos=` |
| `byes` | Who is on bye this week? | `schedule_intelligence.bye_planner` | `/schedule-intelligence#bye-planner` |
| `playoffs` | Will I make the playoffs? | `playoff_race.playoff_race(base=projected)` | `/playoff-race` |

The screen grammar compiles to the screener's own typed grammar (`/api/screener/metrics`): metric synonyms map to
registry keys only when they mean exactly that metric ("points per game" alone asks for "PPR points per game": the
screener is full PPR whatever the league scores), operators map to `gt`/`gte`/`lt`/`lte` (`5+`, `5 or more` = `gte`),
values stay inside each metric's bounds, `and` joins a group and `or` starts a new one (`(a AND b) OR c`, the
screener's precedence, echoed back as its expression text). A percentage metric given as a fraction (`0.8`) asks
"did you mean 80%?".

## Card (`ask/v1`)

`status`: `answered` | `clarify` | `abstain` | `unavailable`. `intent {id, label}`, `interpretation` (how the
question was read, with the screener expression and exact week window), `answer {headline, facts[{label, value,
unit}], columns, rows[{cells, player_id?}], truncated}`, `notes` (the handler's own limitations, never new claims),
`provenance` (the handler's `signal-provenance.v1` block when it has one) or `freshness`, `link {path, query}` to the
page that shows the same view, `clarify {code, question, reason, options[{name, position, pro_team, fantasy_team,
ask}]}`, `abstain {code, reason}`, `examples` (supported question shapes, on abstain and clarify).

Wording rules: the playoff card says "deterministic scenario, not a probability" (the playoff race computes no
probability); Start/Sit, matchup and trade cards report projections as projections; nothing explains why a player
scored what he did, and no observational difference is described as a cause (such questions abstain).

## Evaluation (tests/test_ask.py)

92 synthetic phrasings with their expected outcome; the test asserts each one exactly.

| | Phrasings | Correct | Answered as another intent |
|---|---|---|---|
| Supported (14 intents) | 67 | 67 | 0 |
| Abstain (unsupported, unknown metric, out of bounds, unit mismatch, metric/position, other league, other team, other season, same player, empty) | 23 | 23 | 0 (no answer) |
| Clarify (vague metric, fraction for a percentage) | 2 | 2 | 0 |

Per intent: screen 12, screen_preset 3, waivers_best 7, start_sit 7, replacement 4, start_choice 2, matchup 5,
player 5, ownership 5, compare 3, trade_value 3, trade_values_top 3, byes 4, playoffs 4.

Cards are checked against their endpoints on the E2E fixture (screen rows and metric values, Start/Sit summary,
alternatives deltas, matchup totals, trade value row, roster status, compare summary, bye week, playoff places,
waivers Best view), plus: a missing metric is undecided and never 0, same-name and accented names, a hostile player
name returned as inert data, no team in view, an unknown week, the length bound and at most one handler call per
question (two for a week window). `tests/test_ask_scope.py` covers `/v2` authorization before the question is read,
the team in view and colliding league ids.

## Limitations

- English only, and only the phrasings the grammar lists; anything else abstains with the supported shapes.
- One league, the synced season, the team in view; questions about another league, team or season abstain.
- The card shows at most five rows; the link opens the full page.
- No question-level budget beyond the existing web rate limit and the one-call bound: each question costs exactly
  the handler it compiles to.
- No optional local model. A future one may only propose a question for this same grammar and validator.
