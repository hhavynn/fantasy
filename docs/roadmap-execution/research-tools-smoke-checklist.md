# Research tools: production smoke checklist (PR #341, merge 97e2fa9)

Production: frontend `https://fantasy-intel.vercel.app`, API `https://fantasy-intel-api.vercel.app`.
Replace `<PRIVATE>` with a private league you belong to, `<OTHER>` with a private league you do **not** belong to,
and `<PUBLIC>` with a public league, if you have one.

## A. Signed in as a normal (non-admin) member of `<PRIVATE>`

| # | Open | Expect |
|---|---|---|
| A1 | `/l/<PRIVATE>/research` | Research Lab loads. WR/RB/TE sections have data or a plain "not available yet" note. No "Run scripts/…" text. |
| A2 | `/l/<PRIVATE>/research/wr?min_volume=0&min_games=1` | List loads. If there are more than 200 players, the pool line says some are "past the list's limit". |
| A3 | `/l/<PRIVATE>/research/splits` | Game Splits loads. Pick a player and a teammate: results show. There is **no** "Implied team total/G" row. |
| A4 | `/l/<PRIVATE>/pace-lab` | Pace Lab loads (team pace, neutral pass rate, game script), or a plain "not available yet" note. |
| A5 | Players menu → Research (desktop rail, and the phone menu at a narrow width) | Research lab, Game splits and Pace lab are listed and open the pages above. |
| A6 | `/l/<PRIVATE>/projections/performance` | **Not** available: a not-found or unavailable page, no accuracy report. |
| A7 | Reload A3 with its URL (deep link with filters) | Same player, teammate and window are restored. |

## B. Unauthorized: signed out (private window), or signed in as someone who is **not** in `<OTHER>`

| # | Open | Expect |
|---|---|---|
| B1 | `/l/<OTHER>/research` | League not found, no data. |
| B2 | `/l/<OTHER>/research/splits` and `/l/<OTHER>/pace-lab` | League not found, no data. |
| B3 | API: `https://fantasy-intel-api.vercel.app/v2/leagues/<OTHER>/api/research/opportunity?position=WR` | `404 {"detail":"League not found."}` |
| B4 | Signed out, `/l/<PUBLIC>/research` (only if a public league exists) | Loads, because public leagues are visible to everyone by design. |

## Report back

For each row, pass/fail plus a screenshot or the status code, and anything that shows "Run scripts", a stack trace, or
another league's data.
