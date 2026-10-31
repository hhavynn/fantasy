# Soccer Shorts account: design

Approved by the owner on 2026-10-30. A short-form soccer account built on the existing trend engine and
studio. Nothing publishes automatically: every upload stays a human decision.

## Decisions

| Area | Decision |
|---|---|
| Stats | Layered free sources, without Understat |
| Renderer | A soccer pack inside `studio`, added beside the NFL code. NFL files do not move |
| Parody | Original, labelled cartoon characters. No face-swaps and no cloned player voices |

## Stats sources

| Need | Source | Scope |
|---|---|---|
| Fixtures, tables, event window | football-data.org free plan (10 calls/min, needs a free key) | 12 competitions |
| Premier League players: minutes, form, xG, xA | FPL `bootstrap-static` | Premier League |
| MLS players: minutes, xG, xA | American Soccer Analysis, credited | MLS |
| Team form, team xG, shots, cards | football-data.co.uk CSVs | Leagues it publishes |

La Liga, Bundesliga, Serie A and Ligue 1 carry team stats but no player xG. Understat stays out unless it
confirms permission for this use. FBref (xG removed), FotMob (`/api/*` disallowed by robots.txt) and PitchAPI
(no named data source) are excluded. Statz is for checking a number by hand, never a pipeline input.

Enrichment reads its own `data/soccer/soccer.duckdb`. `fantasy.duckdb` is never touched.

## Trend engine

Soccer gets its own store at `data/social/soccer/social.duckdb` and its own studio history file, because
`source_state` (cursors, intervals, cooldowns) is keyed by collector alone. NFL keeps
`data/social/social.duckdb`. Collectors must not carry NFL defaults into a soccer run.

Entities cover about 126 clubs (top five European leagues and MLS) plus the Champions League. Wikidata (CC0)
supplies aliases, with a hand-written nickname table on top. Ambiguous names ("Spurs", "United", "City") need
a soccer-context check.

## Studio

- New code lives in `studio/soccer/` with its own command, brand, episodes folder and `history.jsonl`.
- Shared code changes only where soccer needs a hook. NFL output stays identical.
- Build order: "every X explained" first as the pipeline test, then parallel universe and cartoon parody.

## Content rules

- No match footage, no club crests, no copyrighted music.
- No face-swaps, no cloned or imitated player voices, no photorealistic depictions of real people.
- Every parody carries a visible and spoken parody label, plus YouTube's altered-content disclosure.
- Invented dialogue must be obviously absurd and never presented as a real quote.
- Real statistics in any format pass the existing claim and number gates.
- Jokes target the moment or the stat line, never the person.

## Delivery

1. Engine isolation, NFL unchanged.
2. Soccer adapter and entities: at least 70% of the top 50 trends resolve to the right club or player by hand check.
3. Stats sync and `enrich`, every number sourced and dated.
4. Soccer pack with "every X explained".
5. Cartoon parody and parallel-universe formats, with QC gates for the content rules.

Then publish 10 Shorts per format and compare the formats with each other.
