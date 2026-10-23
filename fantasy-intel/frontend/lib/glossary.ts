// Plain-language definitions for the terms the pages use, so a definition never depends on a hover tooltip.
// Each page lists the terms it shows in a "Terms on this page" disclosure (components/ui/Glossary.tsx).
// Definitions describe what a number is; they never add a claim the analytics do not make.

export const GLOSSARY = {
  PPR: "Points per reception: the scoring format where each catch is worth points. Your league's own settings decide the exact values.",
  "PPR/g": "Fantasy points scored per game played this season (full PPR, from nflverse).",
  xFP: "Expected fantasy points: what an average player would score with the same targets, carries and field position. It measures the role, not the result.",
  "xFP/g": "Expected fantasy points per game played.",
  FPOE: "Fantasy points over expected: points scored minus expected points (xFP). Positive means the player out-produced his role.",
  "Target share": "The share of his team's targets (passes thrown his way) that went to the player.",
  "Snap share": "The share of his team's offensive snaps the player was on the field for.",
  "Backfield share": "The share of his team's running-back carries the player took.",
  aDOT: "Average depth of target: how far past the line of scrimmage, on average, the passes thrown to him travel.",
  "Air-yard share": "The share of his team's passing air yards that were thrown to the player.",
  EPA: "Expected points added: how much a play changed the offense's expected points. Used here to describe what a defense has allowed.",
  ROS: "Rest of season: the remaining weeks of the fantasy season shown on the page.",
  "Projected points": "A forecast of fantasy points in your league's scoring. ESPN's projection by default; Fantasy Intel's own model only when the Proj switch is set to Ours.",
  Ours: "Fantasy Intel's independent projection model. It is experimental, so ESPN's projection stays the default everywhere.",
  "Projected gain": "How many more projected starting points a change produces than the lineup it is compared with. Gains from separate moves are not added together.",
  "Lineup points": "Points of the starting lineup shown: projected before games, actual once they start. Decision pages use the best legal lineup, so a bench player counts only when he would start.",
  "Ratings (0–100)": "Overall, opportunity and production ratings rank a player within his own position from recent play, in full PPR. 100 is the top of the position; a rating is not a price and not projected points.",
  "Value rating": "A 0–100 rank within the player's own position from recent play. A relative rank, not a trade price and not projected points.",
  "Waiver score": "The waiver engine's 0–10 ranking score. It orders candidates; the Claim, Wait or Ignore recommendation comes from separate evidence.",
  FAAB: "Free agent acquisition budget: the money each team has to bid on waiver claims.",
  FLEX: "A lineup slot any eligible running back, receiver or tight end can fill, per your league's settings.",
  IR: "Injured reserve: a roster slot for injured players that does not use an active roster spot.",
  "Q / D / O": "Injury designations: Questionable, Doubtful and Out. The full word appears on the badge's tooltip and in player details.",
  "Kickoff lock": "Once a player's game has kicked off, his lineup slot can no longer change for that week.",
  Bye: "A week the player's NFL team has no game. A bye is never treated as a projected zero.",
  "Bye cluster": "A week in which two or more of the team's active, lineup-eligible players are on bye. It is listed whether or not a starting slot goes unfilled.",
  "Slot coverage": "Whether a starting slot can be filled from the current roster in a week: covered by a player with a game and no open designation, short when nobody on the roster can fill it, or not confirmed when it depends on a player whose week is unknown.",
  "Streaming baseline": "For a kicker or D/ST in a week: the projection of the third-best free agent at the position. It stands for what a weekly add/drop can usually still get, since other managers stream too.",
  "Strength of schedule": "What the defenses a team or player faces in the shown weeks have allowed so far: pass defense for QB, WR and TE (the same ranking for all three) and run defense for RB. Observed results, never a projection of points.",
  "Neutral baseline": "For one week, the pooled value of every defense that can be ranked before that week. A cell's difference is its opponent's value minus this baseline.",
  "Neutral pass rate": "The share of an offense's plays that were dropbacks while the game was neutral: win probability between 20% and 80% with more than two minutes left in the half. A game with no neutral play has no rate, not 0%.",
  "Game script": "How a game went for the offense, from its final margin: competitive within 8 points, leading when it won by 9 or more, trailing when it lost by 9 or more. It labels the whole game after the fact, not each play.",
  "Shrunk estimate": "A team's own average pulled toward the league average by a fixed number of games or plays stated beside it. With few games it stays close to the league; with many it follows the team.",
  "Plays per drive": "Run and pass plays divided by drives with at least one scrimmage play. A possession with no scrimmage play is not a drive.",
  "Hypothetical scenario": "A game-script mix you choose. Its centre and range come from past games shaped like it; it is not a forecast or a probability for any future game.",
  "Rank gap": "The provider's rank minus Fantasy Intel's (Ours) rank for the same player, within his position or across all positions. Positive means Ours ranks him higher. When one side has no rank, the cell names the missing side in words instead; a gap is never shown as zero for a missing rank.",
  "Floor and ceiling": "The 10th and 90th percentile of a player's points if he plays, from Fantasy Intel's point-distribution model around next week's Ours projection: 80% of outcomes are expected between them. Shown for RB, WR and TE only, and never for the rest of the season.",
  "—": "A dash means the number is missing or not computed, never zero. A measured zero is shown as 0.",
} as const;

export type GlossaryTerm = keyof typeof GLOSSARY;

/**
 * Definitions that change for a league whose provider publishes no projections (Sleeper): there every projection is
 * Fantasy Intel's own. Elsewhere this is GLOSSARY unchanged.
 */
const NO_PROVIDER_PROJECTIONS: Partial<Record<GlossaryTerm, string>> = {
  "Projected points": "A forecast of fantasy points in your league's scoring, from Fantasy Intel's own model: this league's provider publishes no projections.",
  Ours: "Fantasy Intel's independent projection model. It is experimental; this league's provider publishes no projections, so every projection here is Ours.",
};

export function glossaryText(term: GlossaryTerm, providerProjections = true): string {
  return (!providerProjections && NO_PROVIDER_PROJECTIONS[term]) || GLOSSARY[term];
}
