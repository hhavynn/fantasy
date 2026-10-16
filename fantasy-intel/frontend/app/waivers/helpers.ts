
export const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"] as const;
export const RECS = ["ALL", "CLAIM", "WAIT", "IGNORE"] as const;
export const REC_LABEL: Record<string, string> = { ALL: "Any", CLAIM: "Claim", WAIT: "Wait", IGNORE: "Ignore" };

export const VIEWS: Record<string, string> = {
  BEST: "Best moves for this team", FREE_AGENTS: "Free-agent adds", CLAIMS: "Waiver claims",
  STREAMING: "Streaming options", WATCHLIST: "Stashes / watchlist", ALL: "All evaluated",
};

export const ACTIONS: Record<string, string> = {
  ADD_NOW: "Add now", WAIVER_CLAIM: "Worth a waiver claim", PRESERVE_PRIORITY: "Preserve priority",
  MONITOR: "Monitor for more evidence", NOT_AN_UPGRADE: "Not an upgrade", NO_LEGAL_MOVE: "No legal move identified",
  VERIFY_LEGALITY: "Verify roster restrictions", VERIFY_COST: "Verify acquisition rules", SEASON_COMPLETE: "No actionable week remaining",
};

export const PAGE_SIZE = 30;

