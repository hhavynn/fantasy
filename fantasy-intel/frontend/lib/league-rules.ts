// League rules page (GET /api/league-rules, contract league-rules/v1): types and pure helpers. The backend reads the
// league's synced settings only; a value it does not hold arrives as null with a reason, which the page shows as a
// muted dash carrying that reason (never 0).

export const RULES_PATH = "/league-rules";
export const CONTRACT = "league-rules/v1";

/** When the shown settings were captured: the sync time, or "age unknown" when the backend holds none (#168 R1). */
export function capturedAge<T>(capturedAt: string | null | undefined, when: (iso: string) => T): T | string {
  return capturedAt ? when(capturedAt) : "age unknown";
}

export type Fact<T = unknown> = { value: T | null; display: string | null; reason: string | null };

export type EngineStatus = "handled" | "partial" | "unsupported" | "unverified";

export type RulesSlot = {
  code: string;
  name: string;
  count: number;
  kind: "starter" | "bench" | "ir" | "taxi" | "unknown";
  eligible: string[] | null;
  eligible_reason: string | null;
  engine: { status: EngineStatus; note: string };
};

export type ScoringRule = {
  key: string;
  group: string;
  label: string;
  points: number;
  by_slot: { slot: string; points: number }[];
  by_position: { position: string; points: number }[];
  reverse: boolean;
  mapped: boolean;
};

// `code` is the backend's stable id for the rule (league_rules.ENGINE_CODES); `rule` is display text only.
export type EngineRule = { code: string; rule: string; status: EngineStatus; detail: string };
export type UnmappedRule = { area: string; key: string; value: string | number | boolean | null; reason: string };

export type LeagueRules = {
  contract: string;
  source: { provider: string; provider_label: string; captured_at: string | null; note: string };
  league: { name: string | null; season: number | null; team_count: Fact<number>; league_type: Fact<string> };
  roster: {
    slots: RulesSlot[];
    starters: Fact<number>;
    bench: number;
    injured_reserve: number;
    taxi: number;
    superflex: boolean;
    flex_types: string[];
    position_limits: Fact<Record<string, number>>;
  };
  scoring: {
    format: Fact<string>;
    points_per_reception: Fact<number>;
    te_premium: Fact<number>;
    te_premium_scope: "slot" | "position" | null;
    scoring_type: Fact<string>;
    highlights: ({ label: string } & Fact<number>)[];
    rules: ScoringRule[];
    zero_rules: number;
    rules_source: string;
  };
  lineup_lock: Fact<string>;
  waivers: { type: Fact<string>; faab_budget: Fact<number>; period: Fact<number>; process_days: Fact<string[]> };
  trades: { deadline: Fact<string | number>; review: Fact<number> };
  playoffs: { teams: Fact<number>; final_week: Fact<number>; regular_season: Fact<number>; start: Fact<number>; round_length: Fact<number> };
  engine_support: EngineRule[];
  unmapped: UnmappedRule[];
};

/** The payload when it is the contract this page reads; anything else is treated as a backend problem. */
export function isLeagueRules(data: unknown): data is LeagueRules {
  if (!data || typeof data !== "object") return false;
  const d = data as Partial<LeagueRules>;
  return d.contract === CONTRACT && !!d.roster && Array.isArray(d.roster.slots) && !!d.scoring
    && Array.isArray(d.engine_support) && Array.isArray(d.unmapped);
}

const STATUS: Record<EngineStatus, { label: string; tone: "good" | "warn" | "bad" | "info" }> = {
  handled: { label: "Handled", tone: "good" },
  partial: { label: "Partly handled", tone: "warn" },
  unsupported: { label: "Not applied", tone: "bad" },
  unverified: { label: "Unverified", tone: "info" },
};

/** Text and badge tone for an engine status; an unknown status reads as unverified rather than handled. */
export function statusLabel(status: string): { label: string; tone: "good" | "warn" | "bad" | "info" } {
  return STATUS[status as EngineStatus] ?? STATUS.unverified;
}

/** How many rules sit in each status, in display order (handled first). Zero counts are left out. */
export function statusCounts(rules: EngineRule[]): { status: EngineStatus; count: number }[] {
  const order: EngineStatus[] = ["handled", "partial", "unsupported", "unverified"];
  return order
    .map((status) => ({ status, count: rules.filter((r) => (STATUS[r.status] ? r.status : "unverified") === status).length }))
    .filter((c) => c.count > 0);
}

/** Who may fill a slot: the eligible positions, or what the slot is for when it is not a starting slot. */
export function eligibleText(slot: RulesSlot): string | null {
  if (slot.eligible === null) return null;
  if (slot.kind === "bench") return "Any rostered player";
  if (slot.kind === "ir") return "Players the provider allows on injured reserve";
  if (slot.kind === "taxi") return "Players the provider allows on the taxi squad";
  return slot.eligible.join(", ");
}

/** A points value as the league awards it: signed for penalties, trimmed decimals, never rounded away. */
export function pointsText(points: number): string {
  const s = Number.isInteger(points) ? String(points) : String(Number(points.toFixed(4)));
  return points > 0 ? `+${s}` : s.replace("-", "−");
}

/** Slot- or position-specific points next to a rule ("TE slot +1.5", "TE +0.5"), or null when the rule is flat. */
export function ruleDetail(rule: ScoringRule): string | null {
  const parts = [
    ...rule.by_slot.map((o) => `${o.slot} slot ${pointsText(o.points)}`),
    ...rule.by_position.map((o) => `${o.position} ${pointsText(o.points)} extra`),
  ];
  if (rule.reverse) parts.push("reverse rule");
  return parts.length ? parts.join(" · ") : null;
}

const GROUP_ORDER = ["Passing", "Rushing", "Receiving", "Bonus", "Miscellaneous", "Kicking", "Team defense", "Unmapped"];

/** Rules grouped by statistic family in a fixed order; groups without rules are dropped. */
export function groupRules(rules: ScoringRule[]): { group: string; rules: ScoringRule[] }[] {
  const groups = new Map<string, ScoringRule[]>();
  for (const r of rules) groups.set(r.group, [...(groups.get(r.group) ?? []), r]);
  const rank = (g: string) => (GROUP_ORDER.includes(g) ? GROUP_ORDER.indexOf(g) : GROUP_ORDER.length);
  return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0])).map(([group, rs]) => ({ group, rules: rs }));
}

/** "9 starters · 6 bench · 2 IR · 1 taxi": the counts the slot table adds up to. */
export function rosterSummary(roster: LeagueRules["roster"]): string {
  const parts: string[] = [];
  if (roster.starters.value !== null) parts.push(`${roster.starters.value} ${roster.starters.value === 1 ? "starter" : "starters"}`);
  if (roster.bench) parts.push(`${roster.bench} bench`);
  if (roster.injured_reserve) parts.push(`${roster.injured_reserve} IR`);
  if (roster.taxi) parts.push(`${roster.taxi} taxi`);
  return parts.join(" · ");
}

/** The one-line answer: scoring format, superflex and the waiver system, only from values the backend holds. */
export function rulesHeadline(d: LeagueRules): string {
  const parts: string[] = [];
  if (d.scoring.format.display) parts.push(d.scoring.format.display);
  if (d.scoring.te_premium.value) parts.push("TE premium");
  if (d.roster.superflex) parts.push("Superflex");
  if (d.waivers.type.value === "faab") parts.push("FAAB waivers");
  else if (d.waivers.type.display) parts.push("Waiver order");
  return parts.length ? parts.join(" · ") : "League rules as synced";
}

/** A value for an unmapped provider setting: verbatim scalars, or null when the backend withheld a structured value. */
export function unmappedValue(value: UnmappedRule["value"]): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}
