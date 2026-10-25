// The one triage list for the UX acceptance gates (e2e/ux.ts; docs/qa/release-review.md).
//
// A11Y_TRIAGE: a violation axe-core reports blocks the gate unless an entry here matches its rule, the route it was
// found on and (when `target` is set) the element's selector. CONTENT_TRIAGE: a leaked raw value or engine identifier
// (expectNoBrokenValues) blocks the gate unless an entry matches the route and the line. Every entry names why it is tolerated and who fixes it. Keep entries
// narrow: a rule tolerated on one route still fails everywhere else, and an entry with a `target` still fails for any
// other element on that route. Remove an entry in the change that fixes it.
//
// Routes are matched against the path and query with a league id replaced by `:league` (/l/:league/matchup).

export type TriageEntry = {
  /** The axe-core rule id (https://dequeuniversity.com/rules/axe/). */
  rule: string;
  /** The routes it is tolerated on. */
  route: RegExp;
  /** Optional: only elements whose axe selector matches. */
  target?: RegExp;
  /** Why it is tolerated for now, and what the fix is. */
  reason: string;
  /** Who owns the fix: the issue that tracks it (and the file that has to change), or "n/a (by design)". */
  owner: string;
};

export const A11Y_TRIAGE: TriageEntry[] = [
  {
    rule: "target-size",
    route: /^(\/l\/:league)?\/trades(\?|$)/,
    target: /^#_R_\w+_-/,
    reason: "The trade builder's 18px player checkboxes sit inside a full-row <label> (44px tall on phones), so the "
      + "row is the real target; axe measures the bare input, and only when a neighbour lands within 24px. Fix: size "
      + "the input's hit area to the row (app/trades/builder.module.css .check) and drop this entry.",
    owner: "#322 (app/trades/builder.module.css)",
  },
  {
    rule: "heading-order",
    route: /^(\/l\/:league)?\/matchup(\?|$)/,
    target: /teamName/,
    reason: "The scoreboard's team names are h3 directly under the page h1 (no h2 between). Fix: make them h2, or "
      + "give the scoreboard a section heading.",
    owner: "#323 (app/matchup/Scoreboard.tsx)",
  },
  {
    rule: "link-in-text-block",
    route: /^(\/l\/:league)?\/players(\?|$)/,
    target: /note > a/,
    reason: "The links in the profile's note lines are told apart from the text by colour only. Fix: underline "
      + "links in .note (app/players/players.module.css).",
    owner: "#324 (app/players/players.module.css)",
  },
];

export type ContentTriageEntry = {
  /** The routes it is tolerated on. */
  route: RegExp;
  /** The offending line of page text. */
  text: RegExp;
  reason: string;
  owner: string;
};

export const CONTENT_TRIAGE: ContentTriageEntry[] = [
  {
    route: /^\/leagues$/,
    text: /ESPN_LEAGUE_ID|ESPN_SEASON|ESPN_S2/,
    reason: "Intended: with multi-league off, /leagues is the deployment owner's setup note and names the .env "
      + "settings to edit.",
    owner: "n/a (by design)",
  },
  {
    route: /^\/l\/:league\/start-sit(\?|$)/,
    text: /\(null\)/,
    reason: "A Sleeper team has no abbreviation, and the page header prints it as \"Sleeper Alpha (null)\". Fix: omit the "
      + "parenthesis when the abbreviation is missing.",
    owner: "#325 (app/start-sit/page.tsx)",
  },
  {
    route: /^\/l\/:league(\/matchup)?(\?|$)/,
    text: /ESPN_MY_TEAM_ID/,
    reason: "With no saved team, a league's Overview (\"...or set ESPN_MY_TEAM_ID in .env and re-sync\") and a Sleeper "
      + "league's Matchup (\"Set ESPN_MY_TEAM_ID or pass team_id\") show the single-league operator hint, an "
      + "environment variable named after ESPN, to readers who can only pick a team. The Matchup line is the backend's "
      + "TeamNotFound detail (backend/app/analytics/matchup.py), printed by the page.",
    owner: "#326 (app/page.tsx; the Matchup copy is backend/app/analytics/matchup.py TeamNotFound)",
  },
];
