// The Overview "Action items" list. It only selects, filters and orders what the backend already returned:
// Start/Sit moves it marks ACTIONABLE (and, separately, the ones it marks locked or blocked), starters the roster feed
// flags as unavailable, empty lineup slots in the optimal projected lineup, waiver pickups the waiver engine marks
// actionable, sell market signals on the roster and the league's trade deadline. No number is computed here.
// `lib/weekly-decisions.ts` turns this list into the Overview's weekly decision hub (evidence, priority, scoped links).

import { providerName } from "./labels.ts";

export type ActionTone = "negative" | "caution" | "info" | "positive";
/** What the decision is about. Injury items are shown with the lineup. */
export type ActionCategory = "lineup" | "waivers" | "trade" | "injury" | "league";
/** actionable: a move the manager can make now; informational: worth knowing, no move is asserted;
 *  locked / unavailable: the engine recommends it but it cannot be made now (`statusReason` says why). */
export type ActionStatus = "actionable" | "informational" | "locked" | "unavailable";
/** high: the submitted lineup loses a whole slot if nothing changes; medium: a projected gain is on offer; low: watch. */
export type ActionUrgency = "high" | "medium" | "low";
/** The payload an item was read from; its evidence is that payload's signal provenance. */
export type ActionSource = "overview" | "matchup" | "startSit" | "waivers";

export type ActionItem = {
  id: string;
  tone: ActionTone;
  title: string;
  reason: string;
  href: string;
  cta: string;
  category: ActionCategory;
  status: ActionStatus;
  /** Why a locked or unavailable item cannot be acted on now; null for the others. */
  statusReason: string | null;
  urgency: ActionUrgency;
  source: ActionSource;
  /** The provenance signals of the source payload that back the item (signal-provenance.v1 keys); null = all of them. */
  signals: string[] | null;
  /** The players the item is about (for a swap: the benched ones first), for duplicate suppression. */
  players: ActionPlayer[];
  /** The lineup slot or position the item is about, when it has one. */
  position: string | null;
};

export type ActionPlayer = { id: number; name: string; slot: string | null };

// Structural inputs: only the fields this module reads, so unrelated pages can change their own types.
export type RosterInput = {
  espn_player_id: number;
  full_name: string;
  position: string;
  lineup_slot: string;
  is_starter: boolean;
  on_bye: boolean | null;
  availability: { status: string; sources: string[] } | null;
  /** The usage model's market signal (BUY / SELL / HOLD / WATCH / MONITOR); absent or null when not classified. */
  market_signal?: string | null;
};

export type MatchupInput = {
  status: string;
  me: { lineup: { slot: string; player_id: number | null }[] };
};

export type StartSitInput = {
  scoring_period: number;
  summary: { starters_count: number; status?: string };
  decisions: {
    id: string;
    action: string;
    actionability: string;
    start_players: { id: number; name: string; position: string }[];
    bench_players: { id: number; name: string; position: string }[];
    target_slot: string | null;
    projected_net_gain: number | null;
    /** The engine's sentences on why a move is locked, blocked or uncertain. */
    warnings?: string[];
  }[];
};

export type WaiverInput = {
  espn_player_id: number;
  full_name: string;
  position: string;
  /** Whether the suggested drop is still on the team's latest synced roster (null: no drop; absent: older backend). */
  drop_on_roster?: boolean | null;
  components: {
    decision?: {
      action: string;
      actionable: boolean;
      roster_move: { summary: string; drop: { name: string } | null };
      utility: { forward: { total_gain: number | null; weeks: unknown[] } };
    };
  };
};

/** The league settings the list reads (from /api/overview). */
export type LeagueInput = {
  /** The provider's trade deadline (ISO timestamp, ESPN); null when the league has none or it was not synced. */
  trade_deadline: string | null;
  /** A week-based trade deadline (Sleeper's setting): trades are taken to be open through that week. Null or absent
   *  when the provider has none or an older backend did not send it. */
  trade_deadline_week?: number | null;
  /** The league's current scoring week, which a week-based deadline is compared with. */
  current_week?: number | null;
};

export type ActionSources = {
  roster: RosterInput[] | null;
  matchup: MatchupInput | null;
  startSit: StartSitInput | null;
  /** The BEST view of /api/waivers: a few rows plus `total`, the number of actionable pickups overall. */
  waivers: { players: WaiverInput[]; total: number } | null;
  /** The league's provider, where a lineup is set ("espn" unless given). */
  provider?: string;
  /** League settings from the overview; null or absent when it did not load. */
  league?: LeagueInput | null;
  /** The time the deadline is compared with (ms since epoch); defaults to now. */
  now?: number;
};

export type ActionList = {
  items: ActionItem[];
  /** Recommended moves that cannot be made now (a game has kicked off, an IR slot is in the way, the lock state or a
   *  drop could not be verified), kept apart so they never read as something to do. */
  blocked: ActionItem[];
  /** Actionable Start/Sit moves and waiver pickups beyond the caps (they live on their own pages). */
  more: { swaps: number; waivers: number };
  /** Sources that failed to load, by name, so the list never claims "all clear" on partial knowledge. */
  unavailable: string[];
  /** Sources that loaded but say their own data is incomplete (Start/Sit's `incomplete_data`), with why: an empty
   *  list is then not an all-clear either. */
  partial: { source: string; reason: string }[];
};

const SEVERE: Record<string, string> = {
  OUT: "out", IR: "on injured reserve", SUSPENDED: "suspended", DOUBTFUL: "doubtful",
  // The raw provider spellings (ESPN's vocabulary, which Sleeper statuses are mapped to).
  INJURY_RESERVE: "on injured reserve", SUSPENSION: "suspended",
};
const SLOT_POSITIONS = new Set(["QB", "RB", "WR", "TE"]);
/** Why a lineup alert cannot be acted on when Start/Sit says every rostered player's game has kicked off. */
export const ALL_LOCKED_REASON = "Every game on the roster has kicked off, so no lineup change can be made this week.";
const DAY_MS = 864e5;
/** A deadline closer than this is worth a line on the Overview. */
export const DEADLINE_NOTICE_DAYS = 7;

export const joinNames = (names: string[]) => (names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`);

function pickupHref(slot: string): string {
  if (slot === "D/ST" || slot === "K") return `/waivers?view=STREAMING&pos=${encodeURIComponent(slot)}`;
  if (SLOT_POSITIONS.has(slot)) return `/waivers?pos=${slot}`;
  return "/waivers";
}

type Base = Omit<ActionItem, "statusReason" | "players" | "position" | "signals"> &
  Partial<Pick<ActionItem, "statusReason" | "players" | "position" | "signals">>;
const item = (b: Base): ActionItem => ({ statusReason: null, players: [], position: null, signals: null, ...b });

/** The trade deadline as a time, or null when there is none or it does not parse. */
function deadlineTime(league: LeagueInput | null | undefined): number | null {
  const raw = league?.trade_deadline?.trim().replace(" ", "T");
  if (!raw) return null;
  // The syncs write naive UTC times.
  const t = Date.parse(/[zZ]$|[+-]\d\d:?\d\d$/.test(raw) || !raw.includes("T") ? raw : `${raw}Z`);
  return Number.isFinite(t) ? t : null;
}

const rosterPlayer = (p: RosterInput): ActionPlayer => ({ id: p.espn_player_id, name: p.full_name, slot: p.lineup_slot });

/** One item for every questionable starter (a game-time decision each, so one line rather than a card per player). */
export function questionableItem(players: ActionPlayer[]): ActionItem {
  const one = players.length === 1;
  return item({
    id: "questionable", tone: "caution",
    title: one ? `${players[0].name} is questionable` : `${players.length} starters are questionable`,
    reason: one ? `Starting at ${players[0].slot}.` : `${joinNames(players.map((p) => p.name))}.`,
    href: "/start-sit", cta: "Start/Sit",
    category: "injury", status: "informational", urgency: "low", source: "overview", signals: ["league", "injuries"], players,
  });
}

export function buildActionItems(src: ActionSources, caps: { swaps?: number; waivers?: number } = {}): ActionList {
  const maxSwaps = caps.swaps ?? 3;
  const maxWaivers = caps.waivers ?? 2;
  const now = src.now ?? Date.now();
  const weekOver = src.matchup?.status === "final";
  const unavailable: string[] = [];
  if (!src.roster) unavailable.push("Roster");
  if (!src.matchup) unavailable.push("Matchup");
  if (!src.startSit) unavailable.push("Start/Sit");
  if (!src.waivers) unavailable.push("Waivers");

  const severe: ActionItem[] = [];
  const questionable: RosterInput[] = [];
  if (src.roster && !weekOver) {
    for (const p of src.roster) {
      if (!p.is_starter) continue;
      const status = p.availability?.status ?? "AVAILABLE";
      const source = p.availability?.sources[0];
      if (p.on_bye === true) {
        severe.push(item({
          id: `bye-${p.espn_player_id}`, tone: "negative", title: `${p.full_name} is on bye`,
          reason: `Starting at ${p.lineup_slot}, but his team has no game this week.`, href: "/start-sit", cta: "Start/Sit",
          category: "lineup", status: "actionable", urgency: "high", source: "overview", signals: ["league"],
          players: [rosterPlayer(p)], position: p.lineup_slot,
        }));
      } else if (status in SEVERE) {
        severe.push(item({
          id: `out-${p.espn_player_id}`, tone: "negative", title: `${p.full_name} is ${SEVERE[status]}`,
          reason: `Starting at ${p.lineup_slot}${source ? ` · ${source}` : ""}.`, href: "/start-sit", cta: "Start/Sit",
          category: "injury", status: "actionable", urgency: "high", source: "overview", signals: ["league", "injuries"],
          players: [rosterPlayer(p)], position: p.lineup_slot,
        }));
      } else if (status === "QUESTIONABLE") {
        questionable.push(p);
      }
    }
  }

  const lineup: ActionItem[] = [];
  const swaps: ActionItem[] = [];
  const blocked: ActionItem[] = [];
  let swapsMore = 0;
  if (src.startSit) {
    if (src.startSit.summary.starters_count === 0) {
      lineup.push(item({
        id: "no-lineup", tone: "caution", title: `No lineup submitted for week ${src.startSit.scoring_period}`,
        reason: `Start/Sit has nothing to compare against until a lineup is set in ${providerName(src.provider)}.`, href: "/start-sit", cta: "Start/Sit",
        category: "lineup", status: "actionable", urgency: "high", source: "startSit", signals: ["league"],
      }));
    } else {
      const improves = (d: StartSitInput["decisions"][number]) =>
        d.action !== "NO_CHANGE" && (d.projected_net_gain === null || d.projected_net_gain > 0);
      const byGain = (a: StartSitInput["decisions"][number], b: StartSitInput["decisions"][number]) =>
        (b.projected_net_gain ?? -Infinity) - (a.projected_net_gain ?? -Infinity);
      const describe = (d: StartSitInput["decisions"][number]) => {
        const where = d.target_slot ? ` at ${d.target_slot}` : "";
        return {
          title: `Start ${joinNames(d.start_players.map((p) => p.name))} over ${joinNames(d.bench_players.map((p) => p.name))}`,
          reason: d.projected_net_gain !== null ? `+${d.projected_net_gain.toFixed(1)} projected points${where}.` : `Recommended by Start/Sit${where}.`,
          players: [...d.bench_players, ...d.start_players].map((p) => ({ id: p.id, name: p.name, slot: null })),
          position: d.target_slot,
        };
      };
      const moves = src.startSit.decisions.filter((d) => improves(d) && d.actionability === "ACTIONABLE").sort(byGain);
      for (const d of moves.slice(0, maxSwaps)) {
        swaps.push(item({
          id: `swap-${d.id}`, tone: "positive", ...describe(d), href: "/start-sit", cta: "Start/Sit",
          category: "lineup", status: "actionable", urgency: "medium", source: "startSit",
        }));
      }
      swapsMore = Math.max(0, moves.length - maxSwaps);
      // Locked (a game has kicked off), IR-blocked and uncertain (lock state or projections not verified) moves: the
      // engine's own warning is the reason, so nothing here asserts the move can be made.
      for (const d of src.startSit.decisions.filter((x) => improves(x) && ["LOCKED", "IR_BLOCKED", "UNCERTAIN"].includes(x.actionability)).sort(byGain)) {
        const locked = d.actionability === "LOCKED";
        const fallback = locked ? "A player in this move has already kicked off."
          : d.actionability === "IR_BLOCKED" ? "A player in this move is in an IR slot." : "The move could not be verified.";
        blocked.push(item({
          id: `swap-${d.id}`, tone: "caution", ...describe(d), href: "/start-sit", cta: "Start/Sit",
          category: "lineup", status: locked ? "locked" : "unavailable", statusReason: d.warnings?.[0] ?? fallback,
          urgency: "low", source: "startSit",
        }));
      }
    }
  }

  const empty: ActionItem[] = [];
  if (src.matchup && !weekOver) {
    const slots = [...new Set(src.matchup.me.lineup.filter((s) => s.player_id === null).map((s) => s.slot))];
    for (const slot of slots) {
      empty.push(item({
        id: `empty-${slot}`, tone: "caution", title: `Empty ${slot} slot`,
        reason: "No rostered player is projected for it in your best lineup.", href: pickupHref(slot), cta: "Find a pickup",
        category: "lineup", status: "actionable", urgency: "high", source: "matchup", position: slot,
      }));
    }
  }

  const risky: ActionItem[] = questionable.length === 0 ? [] : [questionableItem(questionable.map(rosterPlayer))];

  const pickups: ActionItem[] = [];
  let waiversMore = 0;
  if (src.waivers) {
    const actionable = src.waivers.players.filter((w) => w.components.decision?.actionable);
    // A suggested drop that has left the roster since the analysis ran makes the move unverifiable, not actionable.
    const current = actionable.filter((w) => w.drop_on_roster !== false);
    const listed = current.slice(0, maxWaivers);
    const verb = (action: string) => (action === "ADD_NOW" ? "Add" : action === "WAIVER_CLAIM" ? "Claim" : "Consider");
    for (const w of listed) {
      const d = w.components.decision!;
      const gain = d.utility.forward.total_gain;
      const parts = [
        d.roster_move.drop ? `drop ${d.roster_move.drop.name}` : null,
        gain !== null ? `+${gain.toFixed(1)} projected lineup points over ${d.utility.forward.weeks.length} ${d.utility.forward.weeks.length === 1 ? "week" : "weeks"}` : null,
      ].filter((x): x is string => x !== null);
      pickups.push(item({
        id: `waiver-${w.espn_player_id}`, tone: "info", title: `${verb(d.action)} ${w.full_name} (${w.position})`,
        reason: parts.length ? `${parts.join(" · ")}.` : d.roster_move.summary, href: "/waivers", cta: "Waivers",
        category: "waivers", status: "actionable", urgency: "medium", source: "waivers",
        players: [{ id: w.espn_player_id, name: w.full_name, slot: null }], position: w.position,
      }));
    }
    for (const w of actionable.filter((x) => x.drop_on_roster === false)) {
      const d = w.components.decision!;
      blocked.push(item({
        id: `waiver-${w.espn_player_id}`, tone: "caution", title: `${verb(d.action)} ${w.full_name} (${w.position})`,
        reason: d.roster_move.drop ? `The analysis pairs it with dropping ${d.roster_move.drop.name}.` : d.roster_move.summary,
        href: "/waivers", cta: "Waivers", category: "waivers", status: "unavailable",
        statusReason: "The suggested drop is no longer on the roster; the waiver analysis predates the latest roster sync.",
        urgency: "low", source: "waivers", players: [{ id: w.espn_player_id, name: w.full_name, slot: null }], position: w.position,
      }));
    }
    waiversMore = Math.max(0, Math.max(src.waivers.total, actionable.length) - listed.length - (actionable.length - current.length));
  }

  // Trades: the usage model's SELL signal on the roster (one item for all of them), while trades are still open, and
  // the deadline itself when it is close. Both are for awareness; neither asserts a trade.
  const trade: ActionItem[] = [];
  const league: ActionItem[] = [];
  const deadline = deadlineTime(src.league);
  // A week-based deadline (Sleeper) is used only without a timestamp, and only with a known current week: trades count
  // as closed once the league is past that week (not during it: the cut-off time within the week is not synced).
  const deadlineWeek = deadline === null && src.league?.trade_deadline_week != null && src.league.current_week != null
    ? { week: src.league.trade_deadline_week, current: src.league.current_week } : null;
  const tradesClosed = (deadline !== null && deadline <= now) || (deadlineWeek !== null && deadlineWeek.current > deadlineWeek.week);
  const sells = (src.roster ?? []).filter((p) => p.market_signal === "SELL");
  if (sells.length > 0 && !tradesClosed) {
    trade.push(item({
      id: "sell-signal", tone: "info",
      title: sells.length === 1 ? `Sell signal on ${sells[0].full_name}` : `${sells.length} players have a sell signal`,
      reason: `${sells.length === 1 ? `${sells[0].position}, ${sells[0].is_starter ? `starting at ${sells[0].lineup_slot}` : "on the bench"}` : joinNames(sells.map((p) => p.full_name))}`
        + ". The usage model's market signal, from role and production against expected points.",
      href: "/trades/discover", cta: "Discover trades",
      category: "trade", status: "informational", urgency: "low", source: "overview", signals: ["league", "usage"],
      players: sells.map(rosterPlayer),
    }));
  }
  if (deadline !== null && !tradesClosed && deadline - now <= DEADLINE_NOTICE_DAYS * DAY_MS) {
    const days = Math.floor((deadline - now) / DAY_MS);
    league.push(item({
      id: "trade-deadline", tone: "info",
      title: days < 1 ? "The trade deadline is within a day" : `The trade deadline is in ${days} ${days === 1 ? "day" : "days"}`,
      reason: `From the league settings in ${providerName(src.provider)}.`, href: "/trades/discover", cta: "Discover trades",
      category: "league", status: "informational", urgency: "low", source: "overview", signals: ["league"],
    }));
  }
  if (deadlineWeek !== null && !tradesClosed && deadlineWeek.week - deadlineWeek.current <= 1) {
    const thisWeek = deadlineWeek.week === deadlineWeek.current;
    league.push(item({
      id: "trade-deadline", tone: "info",
      title: `The trade deadline is ${thisWeek ? "this week" : "next week"} (week ${deadlineWeek.week})`,
      reason: `From the league settings in ${providerName(src.provider)}; the cut-off time within the week is not synced.`,
      href: "/trades/discover", cta: "Discover trades",
      category: "league", status: "informational", urgency: "low", source: "overview", signals: ["league"],
    }));
  }

  const items = [...severe, ...lineup, ...swaps, ...empty, ...risky, ...pickups, ...trade, ...league];
  // Start/Sit's summary status is the engine's own word on the week: with every game locked no lineup alert can be
  // acted on, a questionable starter included (they move to `blocked`, with the reason), and incomplete projections
  // make an empty list partial rather than an all-clear.
  const summary = src.startSit?.summary.status;
  const lockedLineup = summary === "all_locked"
    ? items.filter((i) => i.category === "lineup" || i.category === "injury") : [];
  const partial = summary === "incomplete_data"
    ? [{ source: "Start/Sit", reason: "Some players have no projection, so Start/Sit claims no gain for this lineup." }] : [];
  return {
    items: items.filter((i) => !lockedLineup.includes(i)),
    blocked: [
      ...lockedLineup.map((i) => ({ ...i, status: "locked" as const, statusReason: ALL_LOCKED_REASON, tone: "caution" as const, urgency: "low" as const })),
      ...blocked,
    ],
    more: { swaps: swapsMore, waivers: waiversMore },
    unavailable,
    partial,
  };
}
