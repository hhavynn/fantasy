// The Overview's weekly decision hub (issue #165): "what should I do before kickoff?" for one league, team and week.
// It turns the action list (`buildActionItems`, which only selects what the backend already recommends) into typed
// `WeeklyDecision`s: each carries the provenance of the payload it was read from (signal-provenance.v1), a status that
// says whether it can be acted on now, and a link that keeps the league, team and week. Duplicates are folded into the
// decision that resolves them, actionable decisions come first, one is the primary next step and at most five are shown.
// No number is computed here and nothing is added that the sources did not say. Pure functions so node --test covers them.

import {
  questionableItem, type ActionCategory, type ActionItem, type ActionList, type ActionSource, type ActionStatus,
  type ActionTone, type ActionUrgency,
} from "./action-items.ts";
import { withLeague } from "./league-scope.ts";
import { plural } from "./plural.ts";
import type { ProjectionSource } from "./projection-source.ts";
import type { SignalProvenance } from "./signal-provenance.ts";
import { withTeam } from "./team-perspective.ts";

/** Decisions shown on the Overview, the primary one included. */
export const WEEKLY_DECISION_LIMIT = 5;

/** The league, team, week and projection source a decision was made for; every link out of the hub keeps them. */
export type DecisionContext = {
  /** The league the page is scoped to (`/l/<lg>`), or null on the classic single-league site. */
  league: string | null;
  /** The analyzed team (`?team=`), or null for the owner's default team. */
  team: string | null;
  /** The scoring week the decisions are for; null when no source said. */
  week: number | null;
  /** Whose weekly projections the lineup decisions read. It is the visitor's projection switch (a cookie that
   *  Start/Sit and Matchup read too), so links do not repeat it as a URL parameter. Null when the week has no
   *  projection at all (Start/Sit's "Unavailable"). */
  projectionSource: ProjectionSource | null;
};

export type DecisionEvidence = {
  /** The page whose data the decision restates. */
  source: string;
  /** The signals (signal-provenance.v1) of that payload that back the decision; empty when the backend sent none, which
   *  the page shows as evidence of unknown age, never as current. */
  signals: SignalProvenance[];
  /** The snapshot the payload was read from (the league signal's), or null when unknown. */
  snapshotVersion: string | null;
};

export type WeeklyDecision = {
  id: string;
  category: ActionCategory;
  urgency: ActionUrgency;
  /** actionable | informational | locked | unavailable; locked and unavailable carry `statusReason`. */
  status: ActionStatus;
  statusReason: string | null;
  tone: ActionTone;
  title: string;
  reason: string;
  evidence: DecisionEvidence;
  context: DecisionContext;
  /** The page that holds the evidence and the move, scoped to `context`. */
  href: string;
  cta: string;
};

export type DecisionGroupKey = "lineup" | "waivers" | "trade" | "league";
export type DecisionGroup = { key: DecisionGroupKey; label: string; decisions: WeeklyDecision[] };

export type WeeklyHub = {
  /** The first actionable decision: the one next step. Null when nothing can be acted on. */
  primary: WeeklyDecision | null;
  /** The other decisions, grouped by area, groups in the order of their first decision. */
  groups: DecisionGroup[];
  /** Decisions beyond the limit (and beyond the action list's caps), per destination. */
  more: { key: DecisionGroupKey; count: number; label: string; href: string }[];
  /** Sources that did not load, by name: an empty hub is then not an all-clear. */
  unavailable: string[];
  context: DecisionContext;
  /** Every signal behind the hub, each once (what an all-clear rests on). */
  signals: SignalProvenance[];
};

/** The provenance signals of each payload the action list read. */
export type HubSignals = Partial<Record<ActionSource, readonly SignalProvenance[]>>;

const SOURCE_LABEL: Record<ActionSource, string> = { overview: "Overview", matchup: "Matchup", startSit: "Start/Sit", waivers: "Waivers" };
const GROUP_OF: Record<ActionCategory, DecisionGroupKey> = { lineup: "lineup", injury: "lineup", waivers: "waivers", trade: "trade", league: "league" };
const GROUP_LABEL: Record<DecisionGroupKey, string> = { lineup: "Lineup", waivers: "Waivers", trade: "Trades", league: "League" };
const STATUS_RANK: Record<ActionStatus, number> = { actionable: 0, informational: 1, locked: 2, unavailable: 3 };
const URGENCY_RANK: Record<ActionUrgency, number> = { high: 0, medium: 1, low: 2 };
/** Pages that take `?week=`: a link from the hub opens the week the decision was made for. */
const WEEK_PAGES = new Set(["/start-sit", "/matchup"]);

/**
 * `href` in the decision's context: the week (on pages that take one, unless the href names its own), the team (on
 * team-scoped pages, via `withTeam`) and the league prefix (`/l/<lg>`, via `withLeague`), so a click lands in the same
 * league and team without the proxy's last-viewed-league redirect.
 */
export function decisionHref(href: string, ctx: DecisionContext): string {
  let out = href;
  const [pathAndQuery, hash] = href.split("#");
  const [path, query = ""] = pathAndQuery.split("?");
  if (ctx.week !== null && WEEK_PAGES.has(path)) {
    const qs = new URLSearchParams(query);
    if (!qs.has("week")) qs.set("week", String(ctx.week));
    out = `${path}?${qs}${hash !== undefined ? `#${hash}` : ""}`;
  }
  return withLeague(withTeam(out, ctx.team), ctx.league);
}

/**
 * Folds items that describe the same decision into the one that resolves it:
 * - a starter who is out, doubtful, on IR or on bye and whom an actionable swap benches: the swap carries the reason;
 * - questionable starters an actionable swap benches leave the questionable roll-up (dropped when none is left);
 * - an empty lineup slot with an actionable pickup at that position: the pickup says the slot is empty;
 * - the same id twice (the first is kept).
 */
export function suppressDuplicates(items: readonly ActionItem[]): ActionItem[] {
  const swaps = items.filter((i) => i.source === "startSit" && i.status === "actionable" && i.id.startsWith("swap-"));
  const swapFor = (id: number) => swaps.find((s) => s.players.some((p) => p.id === id));
  const benched = new Set(swaps.flatMap((s) => s.players.map((p) => p.id)));
  const folded = new Map<string, ActionItem[]>();       // swap id -> the alerts it resolves
  const emptyFilled = new Map<string, string>();        // pickup id -> the empty slot it is at
  const out: ActionItem[] = [];
  for (const it of items) {
    if (it.id.startsWith("out-") || it.id.startsWith("bye-")) {
      const swap = swapFor(it.players[0]?.id);
      if (swap) { folded.set(swap.id, [...(folded.get(swap.id) ?? []), it]); continue; }
    }
    if (it.id === "questionable") {
      const left = it.players.filter((p) => !benched.has(p.id));
      if (left.length === 0) continue;
      if (left.length < it.players.length) { out.push(questionableItem(left)); continue; }
    }
    if (it.id.startsWith("empty-") && it.position) {
      const pickup = items.find((p) => p.category === "waivers" && p.status === "actionable" && p.position === it.position && !emptyFilled.has(p.id));
      if (pickup) { emptyFilled.set(pickup.id, it.position); continue; }
    }
    out.push(it);
  }
  const seen = new Set<string>();
  return out.flatMap((it) => {
    if (seen.has(it.id)) return [];
    seen.add(it.id);
    const alerts = folded.get(it.id);
    if (alerts) {
      return [{ ...it, tone: "negative" as const, urgency: "high" as const, reason: `${alerts.map((a) => `${a.title}.`).join(" ")} ${it.reason}` }];
    }
    const slot = emptyFilled.get(it.id);
    if (slot) return [{ ...it, urgency: "high" as const, reason: `Your ${slot} slot is empty. ${it.reason}` }];
    return [it];
  });
}

/** Actionable first, then informational, then locked and unavailable; within each, by urgency, then the list's order. */
export function prioritize<T extends Pick<ActionItem, "status" | "urgency">>(items: readonly T[]): T[] {
  return items.map((it, i) => ({ it, i }))
    .sort((a, b) => STATUS_RANK[a.it.status] - STATUS_RANK[b.it.status] || URGENCY_RANK[a.it.urgency] - URGENCY_RANK[b.it.urgency] || a.i - b.i)
    .map(({ it }) => it);
}

function evidenceFor(it: ActionItem, signals: HubSignals): DecisionEvidence {
  const all = signals[it.source] ?? [];
  const picked = it.signals === null ? [...all] : all.filter((s) => it.signals!.includes(s.signal));
  const version = picked.find((s) => s.signal === "league")?.snapshot_version ?? picked.find((s) => s.snapshot_version)?.snapshot_version ?? null;
  return { source: SOURCE_LABEL[it.source], signals: picked, snapshotVersion: version };
}

function toDecision(it: ActionItem, ctx: DecisionContext, signals: HubSignals): WeeklyDecision {
  return {
    id: it.id, category: it.category, urgency: it.urgency, status: it.status, statusReason: it.statusReason, tone: it.tone,
    title: it.title, reason: it.reason, evidence: evidenceFor(it, signals), context: ctx,
    href: decisionHref(it.href, ctx), cta: it.cta,
  };
}

const MORE: Record<DecisionGroupKey, { href: string; noun: [string, string] } | null> = {
  lineup: { href: "/start-sit", noun: ["lineup decision", "lineup decisions"] },
  waivers: { href: "/waivers", noun: ["waiver pickup", "waiver pickups"] },
  trade: { href: "/trades/discover", noun: ["trade signal", "trade signals"] },
  league: null,
};

/** Every signal of the payloads that loaded, each once (signal, source and label). */
function allSignals(signals: HubSignals): SignalProvenance[] {
  const seen = new Set<string>();
  const out: SignalProvenance[] = [];
  for (const s of (["overview", "matchup", "startSit", "waivers"] as const).flatMap((k) => signals[k] ?? [])) {
    const key = `${s.signal}|${s.source ?? ""}|${s.label}`;
    if (!seen.has(key)) { seen.add(key); out.push(s); }
  }
  return out;
}

export function buildWeeklyHub(list: ActionList, ctx: DecisionContext, signals: HubSignals = {}, limit = WEEKLY_DECISION_LIMIT): WeeklyHub {
  const ordered = prioritize(suppressDuplicates([...list.items, ...list.blocked]));
  const decisions = ordered.map((it) => toDecision(it, ctx, signals));
  const primary = decisions.find((d) => d.status === "actionable") ?? null;
  const rest = decisions.filter((d) => d !== primary);
  const room = Math.max(0, limit - (primary ? 1 : 0));
  const shown = rest.slice(0, room);

  const groups: DecisionGroup[] = [];
  for (const d of shown) {
    const key = GROUP_OF[d.category];
    let g = groups.find((x) => x.key === key);
    if (!g) { g = { key, label: GROUP_LABEL[key], decisions: [] }; groups.push(g); }
    g.decisions.push(d);
  }

  // What the hub leaves out: decisions past the limit that could still be acted on or are worth knowing, plus the
  // action list's own overflow (Start/Sit moves and waiver pickups beyond its caps).
  const over: Record<DecisionGroupKey, number> = { lineup: list.more.swaps, waivers: list.more.waivers, trade: 0, league: 0 };
  for (const d of rest.slice(room)) {
    if (d.status === "actionable" || d.status === "informational") over[GROUP_OF[d.category]] += 1;
  }
  const more = (Object.keys(over) as DecisionGroupKey[]).flatMap((key) => {
    const m = MORE[key];
    const count = over[key];
    if (!m || count === 0) return [];
    return [{ key, count, label: `${count} more ${plural(count, m.noun[0], m.noun[1])}`, href: decisionHref(m.href, ctx) }];
  });

  return { primary, groups, more, unavailable: list.unavailable, context: ctx, signals: allSignals(signals) };
}
