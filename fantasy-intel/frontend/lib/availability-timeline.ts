// Pure helpers for /injury-impact/timeline (issue #176): the response types of
// GET /api/injury-impact/availability-timeline (docs/availability_timeline.md), labels, ordering and time formatting.
// No React and no `@/` imports, so they run under the unit-test runner (node --experimental-strip-types).

export type Level = "suspended" | "ir" | "out" | "doubtful" | "questionable" | "taxi" | "listed" | "unknown";
export type Coverage = "listed" | "not_listed" | "no_team_report" | "not_published" | "no_feed" | "unmapped";
export type SourceType = "official" | "provider" | "third_party" | "schedule";
export type EventKind = "official_report" | "roster_status" | "provider_status" | "third_party" | "kickoff";
export type Relation = "current" | "latest_for_week" | "earlier_week" | "superseded" | "conflicting" | "duplicate"
  | "supplements" | "context";
export type AtBasis = "provider_published" | "retrieved" | "reported_date" | "week_only" | "scheduled";

export type TimelineEvent = {
  id: string;
  kind: EventKind;
  week: number;
  /** UTC ISO time the event is ordered by (null: the source publishes no time); `at_basis` says what it is. */
  at: string | null;
  at_basis: AtBasis;
  published_at: string | null;
  retrieved_at: string | null;
  /** A date-only report (third-party summaries). */
  date: string | null;
  source: { type: SourceType; label: string; url: string | null; urls: string[] };
  title: string;
  detail: string;
  status: string | null;
  practice: { code: string | null; raw: string | null } | null;
  injury: string | null;
  team: string | null;
  relation: Relation;
  replaced_by: string | null;
  after_kickoff: boolean | null;
  return_to_play?: boolean;
};

export type TimelinePlayer = {
  espn_player_id: number;
  gsis_id: string | null;
  full_name: string | null;
  position: string | null;
  team: string | null;
  lineup_slot: string | null;
  is_starter: boolean;
  headline: {
    level: Level; label: string; text: string; sources: string[]; coverage: Coverage;
    disagreement: string | null; conflicting_reports: boolean;
  };
  lineup: { status: string; sources: string[] };
  practice: { code: "DNP" | "LP" | "FP" | "OTHER" | "unknown"; label: string; raw: string | null; scope: string | null; reason: string | null };
  change: { kind: "new_listing" | "status_change" | "dropped_off_report"; from: string | null; to: string | null } | null;
  high_signal: boolean;
  game: { week: number; opponent: string | null; kickoff: string | null; started: boolean; on_bye: boolean | null };
  freshness: { status: string; reason: string; source_used: string; evidence_retrieved_at: string | null } | null;
  events: TimelineEvent[];
};

export type TimelineSource = {
  key: string; type: SourceType; label: string; url: string | null; available: boolean | null;
  retrieved_at: string | null; publication_time: string; text: string;
};

export type TimelineNotice = { tone: "info" | "caution" | "negative"; code: string; text: string };

export type AvailabilityTimelineResponse = {
  version: string;
  season: number;
  week: number;
  generated_at: string;
  window: { from_week: number; to_week: number };
  league_context: { available: boolean; league_name: string | null; my_team_id: number | null; team_name: string | null; perspective: string };
  sources: TimelineSource[];
  practice: { daily_available: boolean; granularity: string; text: string };
  method: Record<string, string>;
  counts: {
    players: number; team_defenses_skipped: number; designated: number; listed: number; unknown: number;
    high_signal: number; practice_known: number; conflicts: number; disagreements: number;
  };
  notices: TimelineNotice[];
  players: TimelinePlayer[];
};

export const SOURCE_TYPE_LABEL: Record<SourceType, string> = {
  official: "Official", provider: "Provider", third_party: "Third party", schedule: "Schedule",
};

const ET = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
});
const DAY = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric" });

/** A UTC ISO time in US Eastern time with its zone ("Oct 23, 4:00 PM EDT"), the zone NFL reports are issued in.
 *  Independent of the server's own time zone. Null or unparseable: null. */
export function formatEt(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? null : ET.format(d).replace(/[\u202f\u00a0]/g, " ");
}

/** What the event's time means, in words, with the time itself. Never invents a time the source does not publish. */
export function eventTimeText(e: TimelineEvent, provider = "ESPN"): string {
  const t = formatEt(e.at);
  switch (e.at_basis) {
    case "provider_published": return `Published ${t}`;
    case "retrieved": return e.kind === "provider_status" ? `Synced ${t} (${provider} publishes no time)` : `Retrieved ${t} (no publication time)`;
    case "scheduled": return `Scheduled ${t}`;
    case "reported_date": return e.date ? `Reported ${DAY.format(new Date(`${e.date}T12:00:00Z`))} (date only)` : `Week ${e.week}`;
    default: return `Week ${e.week} (no time published)`;
  }
}

/** How this event stands against the others: current, replaced by a newer one, conflicting or supplementary. */
export function relationText(e: TimelineEvent, events: TimelineEvent[]): string | null {
  const by = e.replaced_by ? events.find((x) => x.id === e.replaced_by) : undefined;
  switch (e.relation) {
    case "current": return e.kind === "official_report" ? "Current report" : "Current";
    case "superseded": return by ? `Superseded by the ${formatEt(by.at) ?? "newer"} row` : "Superseded";
    case "earlier_week": return by ? `Replaced by the week ${by.week} report` : e.kind === "official_report" ? "Not on any later report" : null;
    case "conflicting": return "Conflicts with the current row (no time to order them)";
    case "duplicate": return "Same as the current row";
    case "supplements": return "Interpretation: supplements, never replaces, a designation";
    default: return null;
  }
}

/** Events newest first (the API sends them oldest first). */
export function newestFirst(events: TimelineEvent[]): TimelineEvent[] {
  return [...events].reverse();
}

/** The badge value for a level (Badge renders letter codes for Q / D / O / IR). */
export function levelBadge(level: Level): string | null {
  switch (level) {
    case "questionable": return "QUESTIONABLE";
    case "doubtful": return "DOUBTFUL";
    case "out": return "OUT";
    case "ir": return "INJURY_RESERVE";
    case "suspended": return "SUSPENSION";
    default: return null;
  }
}

export function changeText(c: TimelinePlayer["change"]): string | null {
  if (!c) return null;
  if (c.kind === "new_listing") return `Newly on the NFL report: ${c.to}`;
  if (c.kind === "status_change") return `NFL report changed: ${c.from} → ${c.to}`;
  return `Off the NFL report (was ${c.from}): unknown, not confirmed healthy`;
}

/** One line for a high-signal player: the change, else a return to the active roster, else a provider-only designation. */
export function signalText(p: TimelinePlayer): string {
  const c = changeText(p.change);
  if (c) return c;
  const back = p.events.find((e) => e.kind === "roster_status" && e.week === p.game.week && e.return_to_play);
  if (back) return "Back on the active NFL roster this week";
  const roster = p.events.find((e) => e.kind === "roster_status" && e.week === p.game.week);
  if (roster) return roster.title;
  return `${p.headline.label} by the provider; no NFL report yet`;
}

export const PRACTICE_REASON: Record<string, string> = {
  not_in_listing: "His report row has no practice participation.",
  not_listed: "Not on this week's NFL report.",
  no_team_report: "No NFL report for his team this week yet.",
  not_published: "This week's NFL report isn't synced yet.",
  no_feed: "The NFL report has never been synced.",
  unmapped: "No NFL player id, so no report can be read.",
};

/** Injury impact lists designated players with a real role, anchored by NFL id; others go to the page itself. */
export function injuryImpactHref(p: TimelinePlayer): string {
  const designated = !["listed", "unknown", "taxi"].includes(p.headline.level);
  return designated && p.gsis_id ? `/injury-impact#inj-${p.gsis_id}` : "/injury-impact";
}

export function profileHref(p: TimelinePlayer): string {
  return `/players?id=${p.espn_player_id}`;
}
