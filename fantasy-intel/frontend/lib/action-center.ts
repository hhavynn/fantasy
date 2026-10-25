// The all-leagues action center on /leagues (issue #166): one screen of this week's critical decisions across the
// signed-in manager's leagues. The backend (GET /v2/action-center, backend/app/api/action_center.py) enumerates and
// authorizes the caller's own leagues and returns, per league, the payloads the Overview's weekly hub already reads
// (the roster with availability flags, Start/Sit moves, each player's kickoff and the league's lineup-lock rule). This
// module only turns them into decisions with the same contract (`buildActionItems` and `buildWeeklyHub`), marks what a
// kickoff has already locked, and orders the leagues: time to decision first, then severity. No number is computed and
// no league is dropped: a league without data, a team or fresh files says so in words. Pure, so node --test covers it.

import { buildActionItems, questionableItem, type ActionItem, type ActionList, type ActionTone, type ActionUrgency, type RosterInput, type StartSitInput } from "./action-items.ts";
import { leaguePath } from "./league-scope.ts";
import { leagueName, leagueState, parseMyLeague, type MyLeague, type Tone } from "./my-leagues.ts";
import { progressHref } from "./onboarding.ts";
import { plural } from "./plural.ts";
import { signalsOf, type ProvenancePayload, type SignalProvenance } from "./signal-provenance.ts";
import { NO_PROJECTIONS } from "./start-sit-helpers.ts";
import { parseTimestamp } from "./sync-status.ts";
import { buildWeeklyHub, decisionHref, type DecisionContext, type WeeklyDecision, type WeeklyHub } from "./weekly-decisions.ts";

export const ACTION_CENTER_CONTRACT = "action-center.v1";
/** Decisions shown per league card (the next step included); the rest are counted and live on the league's pages. */
export const CARD_DECISIONS = 3;
/** The action center is for managers with this many leagues or more; one league has its Overview. */
export const MIN_LEAGUES = 2;

export type CenterState = "ready" | "not_ready" | "needs_team" | "unavailable";
export type PlayerLock = { lock_status: string | null; kickoff_utc: string | null };
/** The synced lineup-lock rule (league_rules `fact`): a value with its words, or null with the reason it is missing. */
export type LockRule = { value: string | null; display: string | null; reason: string | null };

export type CenterSection = {
  league: MyLeague;
  state: CenterState;
  reason: string | null;
  /** Read from the league's previous files (issue #201): the data may be older than the latest sync. */
  stale: boolean;
  versions: { data_version: string; global_version: string } | null;
  team: { team_id: number; name: string | null } | null;
  week: number | null;
  overview: { roster: RosterInput[]; provenance: ProvenancePayload | null } | null;
  startSit: (StartSitInput & { projection_source: string | null; provenance: ProvenancePayload | null }) | null;
  lock: { next_kickoff_utc: string | null; starters: Record<"unlocked" | "locked" | "unknown" | "no_game", number>; players: Record<string, PlayerLock>; rule: LockRule | null } | null;
};

export type ActionCenterPayload = {
  leagues: CenterSection[]; more: MyLeague[]; limit: number;
  /** When the backend evaluated the leagues (ms): lock states and "in 3h" are as of then; null when not sent. */
  generatedAt: number | null;
};

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const STATES: readonly CenterState[] = ["ready", "not_ready", "needs_team", "unavailable"];

function parseSection(raw: unknown): CenterSection | null {
  if (!isRecord(raw)) return null;
  const league = parseMyLeague(raw.league);
  if (!league) return null;
  const state = STATES.includes(raw.state as CenterState) ? (raw.state as CenterState) : "unavailable";
  const ov = isRecord(raw.overview) && Array.isArray(raw.overview.roster) ? raw.overview : null;
  const ss = isRecord(raw.start_sit) && Array.isArray(raw.start_sit.decisions) && isRecord(raw.start_sit.summary) ? raw.start_sit : null;
  const lock = isRecord(raw.lock) && isRecord(raw.lock.players) && isRecord(raw.lock.starters) ? raw.lock : null;
  const versions = isRecord(raw.versions) && typeof raw.versions.data_version === "string" && typeof raw.versions.global_version === "string"
    ? { data_version: raw.versions.data_version, global_version: raw.versions.global_version } : null;
  const team = isRecord(raw.team) && typeof raw.team.team_id === "number"
    ? { team_id: raw.team.team_id, name: typeof raw.team.name === "string" ? raw.team.name : null } : null;
  return {
    league, state,
    reason: typeof raw.reason === "string" && raw.reason ? raw.reason : null,
    // Anything but an explicit false is not fresh: an older or partial answer never reads as current.
    stale: raw.stale !== false,
    versions, team,
    week: typeof raw.week === "number" ? raw.week : null,
    overview: state === "ready" && ov ? { roster: ov.roster as RosterInput[], provenance: (ov.provenance ?? null) as ProvenancePayload | null } : null,
    startSit: state === "ready" && ss ? {
      ...(ss as unknown as StartSitInput),
      projection_source: typeof ss.projection_source === "string" ? ss.projection_source : null,
      provenance: (ss.provenance ?? null) as ProvenancePayload | null,
    } : null,
    lock: state === "ready" && lock ? (lock as unknown as CenterSection["lock"]) : null,
  };
}

/** The response, defensively: a section that does not parse is dropped only when it names no league at all. */
export function parseActionCenter(payload: unknown): ActionCenterPayload | null {
  if (!isRecord(payload) || payload.contract !== ACTION_CENTER_CONTRACT || !Array.isArray(payload.leagues)) return null;
  const more = isRecord(payload.more) && Array.isArray(payload.more.leagues) ? payload.more.leagues : [];
  return {
    leagues: payload.leagues.map(parseSection).filter((s): s is CenterSection => s !== null),
    more: more.map(parseMyLeague).filter((l): l is MyLeague => l !== null),
    limit: typeof payload.limit === "number" ? payload.limit : 0,
    generatedAt: typeof payload.generated_at === "string" ? parseTimestamp(payload.generated_at) : null,
  };
}

// ---- when the action center itself did not answer -------------------------------------------------------------------

export type CenterProblem = {
  tone: "caution" | "negative";
  title: string;
  body: string;
  /** Where "Try again" goes: this page, reloaded (the server checks the leagues again). */
  retryHref: string;
};

/**
 * What the page says when GET /v2/action-center did not answer with leagues. A 503 is the server being busy (every
 * build slot taken, or the control plane briefly away): an explicit "try again", never an error page and never an
 * empty list that would read as nothing to do. Anything else is an error, still with a retry.
 */
export function centerProblem(res: { status?: number; error: string }): CenterProblem {
  if (res.status === 503) {
    return {
      tone: "caution", title: "Your leagues could not be checked just now",
      body: "The server is busy checking leagues, so nothing below means there is nothing to do. Try again in a few seconds; each league's Overview still has its own decisions.",
      retryHref: "/leagues#action-center",
    };
  }
  return {
    tone: "negative", title: "This week's decisions could not be loaded",
    body: `${res.error} Your leagues are listed below; each league's Overview still has its own decisions.`,
    retryHref: "/leagues#action-center",
  };
}

// ---- decisions --------------------------------------------------------------------------------------------------------

const LOCKED_REASON = "His game has kicked off, so the lineup slot is locked.";

/**
 * Starter alerts whose player's game has already kicked off cannot be acted on any more: they move to the blocked
 * list as locked (the Overview hides them only once the whole week is final). Questionable starters who are locked
 * leave the questionable roll-up. Start/Sit moves already carry the engine's own lock state.
 */
export function applyLocks(list: ActionList, players: Record<string, PlayerLock>): ActionList {
  const locked = (id: number | undefined) => id !== undefined && players[String(id)]?.lock_status === "LOCKED";
  const items: ActionItem[] = [];
  const blocked = [...list.blocked];
  for (const it of list.items) {
    if ((it.id.startsWith("out-") || it.id.startsWith("bye-")) && locked(it.players[0]?.id)) {
      blocked.push({ ...it, status: "locked", statusReason: LOCKED_REASON, tone: "caution", urgency: "low" });
      continue;
    }
    if (it.id === "questionable") {
      const left = it.players.filter((p) => !locked(p.id));
      if (left.length === 0) continue;
      items.push(left.length === it.players.length ? it : questionableItem(left));
      continue;
    }
    items.push(it);
  }
  return { ...list, items, blocked };
}

const PAGE_SOURCES = new Set(["Roster", "Start/Sit"]);

/** The league's decisions with the Overview's contract, from the two payloads the action center reads, and the action
 *  list behind them (its items name the players each decision is about). Null when the league has no data to read. */
export function sectionHub(s: CenterSection): { hub: WeeklyHub; list: ActionList } | null {
  if (s.state !== "ready") return null;
  const built = buildActionItems({ provider: s.league.provider, roster: s.overview?.roster ?? null, startSit: s.startSit, matchup: null, waivers: null });
  // The matchup and waivers are not read here by design (they stay on the league's pages); only a source that was read
  // and failed is reported as missing.
  const list = applyLocks({ ...built, unavailable: built.unavailable.filter((u) => PAGE_SOURCES.has(u)), more: { swaps: built.more.swaps, waivers: 0 } },
    s.lock?.players ?? {});
  const hub = buildWeeklyHub(list, sectionContext(s), {
    overview: signalsOf(s.overview?.provenance), startSit: signalsOf(s.startSit?.provenance),
  }, CARD_DECISIONS);
  return { hub, list };
}

export { NO_PROJECTIONS };

/** The card's projection note: whose projections Start/Sit used, or that the week has none. A league without numbers
 * never reads as using a provider's projections. Null when Start/Sit did not answer. */
export function projectionsNote(label: string | null | undefined): string | null {
  if (!label) return null;
  return label === NO_PROJECTIONS ? "no projections for this week" : `${label} projections`;
}

/** The decision context's projection source from Start/Sit's label: null when the week has no projection at all
 * ("Unavailable"), never a provider's. */
export function projectionSourceOf(label: string | null | undefined): DecisionContext["projectionSource"] {
  if (label === NO_PROJECTIONS) return null;
  return label === "Independent model" ? "ours" : "espn";
}

export function sectionContext(s: CenterSection): DecisionContext {
  return {
    league: s.league.id,
    team: s.team ? String(s.team.team_id) : null,
    week: s.week,
    projectionSource: projectionSourceOf(s.startSit?.projection_source),
  };
}

/** The earliest kickoff still ahead among a decision's players, else the league's next kickoff; null when unknown. */
export function decisionDeadline(d: WeeklyDecision, item: Pick<ActionItem, "players">, s: CenterSection): number | null {
  const players = s.lock?.players ?? {};
  const times = item.players.map((p) => players[String(p.id)])
    .filter((p): p is PlayerLock => !!p && p.lock_status === "UNLOCKED")
    .map((p) => parseTimestamp(p.kickoff_utc)).filter((t): t is number => t !== null);
  if (times.length) return Math.min(...times);
  return d.status === "actionable" ? parseTimestamp(s.lock?.next_kickoff_utc) : null;
}

// ---- cards ----------------------------------------------------------------------------------------------------------

export type CenterCard = {
  id: string;
  name: string;
  league: MyLeague;
  state: CenterState;
  /** The card's status in words (never by colour alone): "Not fresh" for a degraded read. */
  status: { label: string; tone: Tone; detail: string };
  /** Why there is nothing to decide here, for the not-ready states. */
  reason: string | null;
  /** The next step first, then the other decisions, at most CARD_DECISIONS. */
  decisions: { decision: WeeklyDecision; deadline: number | null }[];
  /** Decisions beyond the card, with the link to the page that holds them. */
  more: { count: number; href: string } | null;
  /** Sources that were read and failed: the card is not an all-clear. */
  unavailable: string[];
  /** Sources that loaded but say their data is incomplete (Start/Sit's `incomplete_data`), with why: not an all-clear. */
  partial: { source: string; reason: string }[];
  /** When the next decision closes: the earliest kickoff among its players (or the league's next kickoff). */
  deadline: number | null;
  severity: number;
  nextKickoff: number | null;
  lockRule: string;
  /** Starters whose lock state could not be determined. */
  lockUnknown: number;
  week: number | null;
  teamName: string | null;
  /** "ESPN projections", "no projections for this week" (projectionsNote); null when Start/Sit did not answer. */
  projections: string | null;
  signals: SignalProvenance[];
  versions: CenterSection["versions"];
  committedAt: string | null;
  href: string;
  cta: string;
};

const URGENCY: Record<ActionUrgency, number> = { high: 0, medium: 1, low: 2 };
const TONE: Record<ActionTone, number> = { negative: 0, caution: 1, positive: 2, info: 3 };

function lockRuleText(rule: LockRule | null, provider: string): string {
  if (rule?.display) return rule.display;
  return `${rule?.reason ?? "The lineup-lock rule is not synced."} Times shown are kickoffs; check ${provider === "sleeper" ? "Sleeper" : "ESPN"} for this league's lock.`;
}

function cardStatus(s: CenterSection): CenterCard["status"] {
  if (s.state === "ready" && s.stale) {
    return { label: "Not fresh", tone: "warn", detail: "The newest sync could not be loaded in time, so this reads the league's previous data." };
  }
  if (s.state === "needs_team") return { label: "Team not chosen", tone: "warn", detail: s.reason ?? "Choose your team for this league." };
  if (s.state === "unavailable") return { label: "Data unavailable", tone: "bad", detail: s.reason ?? "This league's data could not be read." };
  if (s.state === "not_ready" && s.league.syncState === "ready") {
    return { label: "Still loading", tone: "warn", detail: s.reason ?? "This league's data did not load in time." };
  }
  return leagueState(s.league);
}

export function buildCard(s: CenterSection): CenterCard {
  const built = sectionHub(s);
  const hub = built?.hub ?? null;
  const ctx = sectionContext(s);
  const items = hub ? [...(hub.primary ? [hub.primary] : []), ...hub.groups.flatMap((g) => g.decisions)] : [];
  // A decision's deadline is its players' kickoff: the action list's item with the same id names them.
  const playersOf = (id: string) => [...(built?.list.items ?? []), ...(built?.list.blocked ?? [])].find((i) => i.id === id) ?? { players: [] };
  const decisions = items.slice(0, CARD_DECISIONS).map((d) => ({ decision: d, deadline: decisionDeadline(d, playersOf(d.id), s) }));
  const actionable = decisions.filter((x) => x.decision.status === "actionable");
  const deadlines = actionable.map((x) => x.deadline).filter((t): t is number => t !== null);
  const first = actionable[0]?.decision ?? null;
  const moreCount = hub ? hub.more.reduce((n, m) => n + m.count, 0) : 0;
  const href = s.state === "ready" ? decisionHref("/", ctx)
    : s.state === "needs_team" ? `/leagues/${s.league.id}`
      : s.league.syncState === "ready" ? leaguePath(s.league.id, "/") : progressHref(s.league.id);
  return {
    id: s.league.id,
    name: leagueName(s.league),
    league: s.league,
    state: s.state,
    status: cardStatus(s),
    reason: s.state === "ready" ? null : s.reason,
    decisions,
    more: moreCount > 0 ? { count: moreCount, href: decisionHref("/start-sit", ctx) } : null,
    unavailable: hub?.unavailable ?? [],
    partial: hub?.partial ?? [],
    deadline: deadlines.length ? Math.min(...deadlines) : null,
    severity: first ? URGENCY[first.urgency] * 10 + TONE[first.tone] : 99,
    nextKickoff: parseTimestamp(s.lock?.next_kickoff_utc),
    lockRule: lockRuleText(s.lock?.rule ?? null, s.league.provider),
    lockUnknown: s.lock?.starters.unknown ?? 0,
    week: s.week,
    teamName: s.team?.name ?? null,
    projections: projectionsNote(s.startSit?.projection_source),
    signals: hub?.signals ?? [],
    versions: s.versions,
    committedAt: s.league.committedAt,
    href,
    cta: s.state === "ready" ? "Open league" : s.state === "needs_team" ? "Choose your team" : s.league.syncState === "ready" ? "Open league" : "See sync progress",
  };
}

/**
 * Order: leagues with a decision to make, soonest deadline first (then the more severe), then those with a decision
 * whose deadline is unknown; then leagues whose data is not ready or not complete (never hidden, never an all-clear);
 * then leagues with nothing flagged, by their next kickoff. Ties keep the backend's order.
 */
export function tier(c: CenterCard): number {
  const acting = c.decisions.some((x) => x.decision.status === "actionable");
  if (acting && c.deadline !== null) return 0;
  if (acting) return 1;
  if (c.state !== "ready" || c.unavailable.length > 0 || c.partial.length > 0 || c.status.tone !== "good") return 2;
  return 3;
}

export function rankCards(cards: readonly CenterCard[]): CenterCard[] {
  const far = Number.POSITIVE_INFINITY;
  return cards.map((c, i) => ({ c, i })).sort((a, b) => {
    const ta = tier(a.c), tb = tier(b.c);
    if (ta !== tb) return ta - tb;
    if (ta === 0 && a.c.deadline !== b.c.deadline) return (a.c.deadline ?? far) - (b.c.deadline ?? far);
    if (ta <= 1 && a.c.severity !== b.c.severity) return a.c.severity - b.c.severity;
    if (ta === 3 && a.c.nextKickoff !== b.c.nextKickoff) return (a.c.nextKickoff ?? far) - (b.c.nextKickoff ?? far);
    return a.i - b.i;
  }).map(({ c }) => c);
}

export function buildActionCenter(p: ActionCenterPayload): { cards: CenterCard[]; more: MyLeague[]; moreLabel: string | null } {
  const n = p.more.length;
  return {
    cards: rankCards(p.leagues.map(buildCard)),
    more: p.more,
    moreLabel: n > 0 ? `${n} more ${plural(n, "league")} not checked here` : null,
  };
}

// ---- words ----------------------------------------------------------------------------------------------------------

/** "in 3h 20m", "in 2d", "under a minute"; "passed" once it is behind `now`. */
export function untilText(t: number, now: number): string {
  const mins = Math.floor((t - now) / 60000);
  if (mins < 0) return "passed";
  if (mins < 1) return "in under a minute";
  if (mins < 60) return `in ${mins}m`;
  const h = Math.floor(mins / 60);
  if (h < 48) return `in ${h}h${mins % 60 ? ` ${mins % 60}m` : ""}`;
  return `in ${Math.floor(h / 24)}d`;
}

/** "Sun, Oct 25, 17:00 UTC": the server renders UTC so the page never claims a time zone it does not know. */
export function kickoffText(t: number): string {
  return `${new Date(t).toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC" })} UTC`;
}

/** The first characters of an opaque version, for display; the whole one goes in a title. */
export function shortVersion(v: string): string {
  return v.length > 22 ? `${v.slice(0, 22)}…` : v;
}
