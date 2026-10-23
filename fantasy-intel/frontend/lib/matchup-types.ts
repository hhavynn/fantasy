// Shape returned by GET /api/matchup (see backend/app/api/matchup.py for the field contract).
import type { ProjectionOrigin, ProjectionSourceDetail } from "./projection-source";
import type { ProvenancePayload } from "./signal-provenance";

export type MatchupStatus = "upcoming" | "in_progress" | "final" | "unknown";
export type MatchupPhase = "regular" | "playoff";

/** A slot in a lineup. `player_id` is null for a slot nobody on the roster can fill. */
export type LineupSlot = {
  slot: string;
  player_id: number | null;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  points: number | null;          // ESPN projection for the week
  is_flex: boolean;
  injury_status: string | null;
  on_bye: boolean | null;         // null = schedule unknown
  projection_origin?: ProjectionOrigin | null;   // set only with source=independent
};

export type BenchPlayer = {
  player_id: number;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  points: number | null;          // null = no ESPN projection (unknown, not zero)
  injury_status: string | null;
  on_bye: boolean | null;
  projection_origin?: ProjectionOrigin | null;
};

/** A player in the lineup ESPN holds (lineup_snapshots). `points` is null until the matchup has started. */
export type SetPlayer = {
  slot: string;
  player_id: number;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  projected_points: number | null;
  points: number | null;
  injury_status: string | null;
  on_bye: boolean | null;
  projection_origin?: ProjectionOrigin | null;
};

export type TeamSide = {
  team_id: number;
  name: string;
  abbrev: string | null;
  record: { wins: number; losses: number; ties: number };
  standing: number | null;
  actual_points: number | null;
  projected_points: number | null;   // null unless the projection is complete
  known_points: number | null;       // optimal lineup from the players that do have a projection
  projection_complete: boolean;
  lineup: LineupSlot[];
  bench: BenchPlayer[];
  actual_lineup: SetPlayer[] | null;
  actual_bench: SetPlayer[] | null;
};

export type Matchup = {
  league: { league_id: number; season: number; name: string; current_week: number };
  week: number;
  phase: MatchupPhase | null;
  matchup_id: number | null;
  /** The matchup period this week belongs to, and its scoring weeks when it spans several. */
  matchup_period: number | null;
  period_weeks: number[] | null;
  is_bye_week: boolean;
  weeks_available: number[];
  me: TeamSide;
  opponent: TeamSide | null;
  status: MatchupStatus;
  notes: string[];
  /** Starting slots no engine fills: every projected total is partial (known points), and a note says so (#253 G3). */
  unsupported_slots?: string[];
  projection_source?: ProjectionSourceDetail;
  /** signal-provenance.v1: league data and the week's projections (lib/signal-provenance.ts). */
  provenance?: ProvenancePayload | null;
};
