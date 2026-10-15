// The Overview "Action items" list. It only selects, filters and orders what the backend already returned:
// Start/Sit moves it marks ACTIONABLE, starters the roster feed flags as unavailable, empty lineup slots in
// the optimal projected lineup, and waiver pickups the waiver engine marks actionable. No number is computed here.

export type ActionTone = "negative" | "caution" | "info" | "positive";

export type ActionItem = {
  id: string;
  tone: ActionTone;
  title: string;
  reason: string;
  href: string;
  cta: string;
};

// Structural inputs: only the fields this module reads, so unrelated pages can change their own types.
export type RosterInput = {
  espn_player_id: number;
  full_name: string;
  position: string;
  lineup_slot: string;
  is_starter: boolean;
  on_bye: boolean | null;
  availability: { status: string; sources: string[] } | null;
};

export type MatchupInput = {
  status: string;
  me: { lineup: { slot: string; player_id: number | null }[] };
};

export type StartSitInput = {
  scoring_period: number;
  summary: { starters_count: number };
  decisions: {
    id: string;
    action: string;
    actionability: string;
    start_players: { id: number; name: string; position: string }[];
    bench_players: { id: number; name: string; position: string }[];
    target_slot: string | null;
    projected_net_gain: number | null;
  }[];
};

export type WaiverInput = {
  espn_player_id: number;
  full_name: string;
  position: string;
  components: {
    decision?: {
      action: string;
      actionable: boolean;
      roster_move: { summary: string; drop: { name: string } | null };
      utility: { forward: { total_gain: number | null; weeks: unknown[] } };
    };
  };
};

export type ActionSources = {
  roster: RosterInput[] | null;
  matchup: MatchupInput | null;
  startSit: StartSitInput | null;
  /** The BEST view of /api/waivers: a few rows plus `total`, the number of actionable pickups overall. */
  waivers: { players: WaiverInput[]; total: number } | null;
};

export type ActionList = {
  items: ActionItem[];
  /** Actionable Start/Sit moves and waiver pickups beyond the caps (they live on their own pages). */
  more: { swaps: number; waivers: number };
  /** Sources that failed to load, by name, so the list never claims "all clear" on partial knowledge. */
  unavailable: string[];
};

const SEVERE: Record<string, string> = { OUT: "out", IR: "on injured reserve", SUSPENDED: "suspended", DOUBTFUL: "doubtful" };
const SLOT_POSITIONS = new Set(["QB", "RB", "WR", "TE"]);

const joinNames = (names: string[]) => (names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`);

function pickupHref(slot: string): string {
  if (slot === "D/ST" || slot === "K") return `/waivers?view=STREAMING&pos=${encodeURIComponent(slot)}`;
  if (SLOT_POSITIONS.has(slot)) return `/waivers?pos=${slot}`;
  return "/waivers";
}

export function buildActionItems(src: ActionSources, caps: { swaps?: number; waivers?: number } = {}): ActionList {
  const maxSwaps = caps.swaps ?? 3;
  const maxWaivers = caps.waivers ?? 2;
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
        severe.push({
          id: `bye-${p.espn_player_id}`, tone: "negative", title: `${p.full_name} is on bye`,
          reason: `Starting at ${p.lineup_slot}, but his team has no game this week.`, href: "/start-sit", cta: "Start/Sit",
        });
      } else if (status in SEVERE) {
        severe.push({
          id: `out-${p.espn_player_id}`, tone: "negative", title: `${p.full_name} is ${SEVERE[status]}`,
          reason: `Starting at ${p.lineup_slot}${source ? ` · ${source}` : ""}.`, href: "/start-sit", cta: "Start/Sit",
        });
      } else if (status === "QUESTIONABLE") {
        questionable.push(p);
      }
    }
  }

  const lineup: ActionItem[] = [];
  const swaps: ActionItem[] = [];
  let swapsMore = 0;
  if (src.startSit) {
    if (src.startSit.summary.starters_count === 0) {
      lineup.push({
        id: "no-lineup", tone: "caution", title: `No lineup submitted for week ${src.startSit.scoring_period}`,
        reason: "Start/Sit has nothing to compare against until a lineup is set in ESPN.", href: "/start-sit", cta: "Start/Sit",
      });
    } else {
      const moves = src.startSit.decisions
        .filter((d) => d.action !== "NO_CHANGE" && d.actionability === "ACTIONABLE" && (d.projected_net_gain === null || d.projected_net_gain > 0))
        .sort((a, b) => (b.projected_net_gain ?? -Infinity) - (a.projected_net_gain ?? -Infinity));
      for (const d of moves.slice(0, maxSwaps)) {
        const where = d.target_slot ? ` at ${d.target_slot}` : "";
        swaps.push({
          id: `swap-${d.id}`, tone: "positive",
          title: `Start ${joinNames(d.start_players.map((p) => p.name))} over ${joinNames(d.bench_players.map((p) => p.name))}`,
          reason: d.projected_net_gain !== null ? `+${d.projected_net_gain.toFixed(1)} projected points${where}.` : `Recommended by Start/Sit${where}.`,
          href: "/start-sit", cta: "Start/Sit",
        });
      }
      swapsMore = Math.max(0, moves.length - maxSwaps);
    }
  }

  const empty: ActionItem[] = [];
  if (src.matchup && !weekOver) {
    const slots = [...new Set(src.matchup.me.lineup.filter((s) => s.player_id === null).map((s) => s.slot))];
    for (const slot of slots) {
      empty.push({
        id: `empty-${slot}`, tone: "caution", title: `Empty ${slot} slot`,
        reason: "No rostered player is projected for it in your best lineup.", href: pickupHref(slot), cta: "Find a pickup",
      });
    }
  }

  const risky: ActionItem[] = questionable.length === 0 ? [] : [{
    id: "questionable", tone: "caution",
    title: questionable.length === 1 ? `${questionable[0].full_name} is questionable` : `${questionable.length} starters are questionable`,
    reason: questionable.length === 1 ? `Starting at ${questionable[0].lineup_slot}.` : `${joinNames(questionable.map((p) => p.full_name))}.`,
    href: "/start-sit", cta: "Start/Sit",
  }];

  const pickups: ActionItem[] = [];
  let waiversMore = 0;
  if (src.waivers) {
    const actionable = src.waivers.players.filter((w) => w.components.decision?.actionable);
    const listed = actionable.slice(0, maxWaivers);
    for (const w of listed) {
      const d = w.components.decision!;
      const verb = d.action === "ADD_NOW" ? "Add" : d.action === "WAIVER_CLAIM" ? "Claim" : "Consider";
      const gain = d.utility.forward.total_gain;
      const parts = [
        d.roster_move.drop ? `drop ${d.roster_move.drop.name}` : null,
        gain !== null ? `+${gain.toFixed(1)} projected lineup points over ${d.utility.forward.weeks.length} ${d.utility.forward.weeks.length === 1 ? "week" : "weeks"}` : null,
      ].filter((x): x is string => x !== null);
      pickups.push({
        id: `waiver-${w.espn_player_id}`, tone: "info", title: `${verb} ${w.full_name} (${w.position})`,
        reason: parts.length ? `${parts.join(" · ")}.` : d.roster_move.summary, href: "/waivers", cta: "Waivers",
      });
    }
    waiversMore = Math.max(0, Math.max(src.waivers.total, actionable.length) - listed.length);
  }

  return { items: [...severe, ...lineup, ...swaps, ...empty, ...risky, ...pickups], more: { swaps: swapsMore, waivers: waiversMore }, unavailable };
}
