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
];
