import assert from "node:assert/strict";
import { test } from "node:test";
import {
  contextText, describeCell, fmtDelta, fmtValue, metricScope, parseSosState, rowLinks, sampleText, sortValue, sosApiPath,
  sosHref, tally, weeksText, type SosCell, type SosRow,
} from "./sos-heatmap.ts";
import { sortRows } from "./sort.ts";

const META = { metric: { key: "pass_epa_per_play", label: "EPA allowed per eligible pass play", unit: "EPA/play", kind: "mean" as const, numerator: "pass_epa_sum", denominator: "pass_plays", definition: "", positions: ["QB", "WR", "TE"] as ("QB" | "RB" | "WR" | "TE")[] }, min_games: 3 };

const graded: SosCell = {
  week: 9, phase: "regular", status: "scheduled", label: "permissive", label_text: "Observed more permissive", reason: null,
  opponent: "KC", home: false, rank: 1, ranked_defenses: 32, value: 0.2, baseline: 0.045, delta: 0.155, games: 8, plays: 320,
};
const thin: SosCell = {
  week: 10, phase: "regular", status: "scheduled", label: "insufficient", label_text: "Insufficient evidence",
  reason: "only 2 presumed-complete game(s) sampled (< 3)", opponent: "NE", home: true, rank: null, ranked_defenses: 31,
  value: -0.06, baseline: 0.04, delta: null, games: 2, plays: 80,
};

function row(over: Partial<SosRow> = {}): SosRow {
  return {
    kind: "player", nfl_team: "KC", team_uncertain: false, notes: [], cells: [graded, thin],
    player: { espn_player_id: 201, name: "Echo RB", position: "RB", pro_team: "KC", injury_status: null, on_ir: false,
      fantasy_team_id: 1, availability_status: "ONTEAM", percent_owned: 50 },
    summary: { weeks: 2, counts: { permissive: 1, middle: 0, tougher: 0, insufficient: 1, unavailable: 0, bye: 0 }, graded_weeks: 1, games: 2, average_rank: 1, average_delta: 0.155 },
    ...over,
  };
}

test("the URL state falls back to defaults for anything unexpected", () => {
  assert.deepEqual(parseSosState({}), { position: "QB", horizon: "ros", view: "teams", team: null, nfl: null, player: null });
  assert.deepEqual(parseSosState({ pos: "wr", horizon: "playoffs", view: "free_agents", team: "3", nfl: "kc", player: "201" }),
    { position: "WR", horizon: "playoffs", view: "free_agents", team: 3, nfl: "KC", player: 201 });
  assert.deepEqual(parseSosState({ pos: "K", horizon: "all", view: "league", team: "x", nfl: "K C", player: "1;2" }),
    { position: "QB", horizon: "ros", view: "teams", team: null, nfl: null, player: null });
});

test("the backend path carries every input that changes the answer; the team only for the roster view", () => {
  assert.equal(sosApiPath(parseSosState({})), "/api/schedule-intelligence/sos?position=QB&horizon=ros&view=teams");
  assert.equal(sosApiPath(parseSosState({ view: "roster", team: "4", pos: "TE", horizon: "playoffs", nfl: "BUF" })),
    "/api/schedule-intelligence/sos?position=TE&horizon=playoffs&view=roster&team_id=4&nfl_team=BUF");
  assert.equal(sosApiPath(parseSosState({ team: "4", player: "102" })),
    "/api/schedule-intelligence/sos?position=QB&horizon=ros&view=teams&player_id=102");
});

test("page links keep sorting, drop defaults and clear the focused player unless it is set", () => {
  const s = parseSosState({ pos: "RB", view: "roster", team: "2", player: "204" });
  assert.equal(sosHref(s, { horizon: "playoffs" }, { sort: "avg", dir: "asc" }),
    "/schedule-intelligence/sos?pos=RB&horizon=playoffs&view=roster&team=2&sort=avg&dir=asc");
  assert.equal(sosHref(s, { player: 205 }), "/schedule-intelligence/sos?pos=RB&view=roster&team=2&player=205");
  assert.equal(sosHref(parseSosState({}), {}), "/schedule-intelligence/sos");
});

test("values and differences are formatted with their sign; missing stays missing, never 0", () => {
  assert.equal(fmtValue(0.0512, "mean"), "+0.051");
  assert.equal(fmtValue(-0.2, "mean"), "−0.200");
  assert.equal(fmtValue(0.215, "rate"), "21.5%");
  assert.equal(fmtValue(null, "mean"), null);
  assert.equal(fmtDelta(0.155, "mean"), "+.155");
  assert.equal(fmtDelta(-0.0312, "mean"), "−.031");
  assert.equal(fmtDelta(0.00001, "mean"), ".000");
  assert.equal(fmtDelta(0.021, "rate"), "+2.1");
  assert.equal(fmtDelta(undefined, "mean"), null);
});

test("a cell's description carries the grade, the baseline difference and the sample in words", () => {
  assert.equal(describeCell(graded, META),
    "Week 9: away at KC. Observed more permissive: rank 1 of 32 on EPA allowed per eligible pass play (+0.200, +.155 from the neutral baseline). Sample: 8 presumed-complete games, 320 plays.");
  const t = describeCell(thin, META);
  assert.match(t, /Not graded: only 2 presumed-complete game/);
  assert.match(t, /Sample: 2 presumed-complete games, 80 plays/);
  assert.doesNotMatch(t, /rank \d/);
  assert.equal(describeCell({ week: 10, phase: null, status: "bye", label: "bye", label_text: "Bye", reason: null }, META), "Week 10: bye.");
  assert.equal(describeCell({ week: 12, phase: null, status: "schedule_missing", label: "unavailable", label_text: "Unavailable",
    reason: "no NFL regular-season schedule is loaded for week 12" }, META), "Week 12: no opponent shown (no NFL regular-season schedule is loaded for week 12).");
});

test("an ungraded cell shows the games sampled against the minimum", () => {
  assert.equal(sampleText(thin, 3), "2 of 3 g");
  assert.equal(sampleText({ ...thin, value: null, games: 0 }, 3), "no data");
  assert.equal(sampleText({ ...thin, games: 5 }, 3), "5 g");
  assert.equal(sampleText({ week: 1, phase: null, status: "bye", label: "bye", label_text: "Bye", reason: null }, 3), null);
});

test("weeks text", () => {
  assert.equal(weeksText([9, 10, 11]), "weeks 9–11");
  assert.equal(weeksText([15]), "week 15");
  assert.equal(weeksText([9, 12]), "weeks 9, 12");
  assert.equal(weeksText([]), "no weeks");
});

test("rows sort on the backend's tallies; a missing average is missing, not 0", () => {
  assert.equal(sortValue(row(), "permissive"), 1);
  assert.equal(sortValue(row(), "avg"), 1);
  assert.equal(sortValue(row({ summary: { ...row().summary, average_rank: null } }), "avg"), null);
  assert.equal(sortValue(row(), "name"), "Echo RB");
  assert.equal(sortValue(row({ player: null, nfl_team: "BUF", kind: "team" }), "name"), "BUF");
});

test("decision links: profile, waivers for an available player, the analyzer for a rostered one", () => {
  assert.deepEqual(rowLinks(row(), "RB", 2), {
    profile: "/players?id=201", waivers: null, trade: { href: "/trades?ta=2&tb=1&b=201", label: "Trade for" },
  });
  assert.deepEqual(rowLinks(row(), "RB", 1).trade, { href: "/trades?ta=1&a=201", label: "Trade away" });
  const fa = row({ player: { ...row().player!, fantasy_team_id: null, availability_status: "WAIVERS" } });
  assert.deepEqual(rowLinks(fa, "RB", 2), { profile: "/players?id=201", waivers: { href: "/waivers?add=201#add-drop", label: "Add/drop" }, trade: null });
  assert.deepEqual(rowLinks(row({ player: null, kind: "team" }), "WR", 2), {
    profile: null, waivers: { href: "/waivers?pos=WR", label: "WR waivers" }, trade: { href: "/trades/values?pos=WR", label: "WR trade values" },
  });
});

test("with no graded week the ▲/▼ counts are unknown (null, sorted last), otherwise out of the graded weeks", () => {
  const none = row({ summary: { ...row().summary, counts: { ...row().summary.counts, permissive: 0, tougher: 0 }, graded_weeks: 0, average_rank: null } });
  assert.equal(tally(none, "permissive"), null);
  assert.equal(tally(none, "tougher"), null);
  assert.equal(sortValue(none, "permissive"), null);
  assert.equal(sortValue(none, "tougher"), null);
  assert.deepEqual(tally(row(), "permissive"), { count: 1, graded: 1 });
  assert.deepEqual(tally(row(), "tougher"), { count: 0, graded: 1 });           // a measured 0 of 1 graded week
  // The default sort (permissive, descending) never ranks unknown zeros above or among measured counts.
  const zero = row({ nfl_team: "BUF", player: null, kind: "team",
    summary: { ...row().summary, counts: { ...row().summary.counts, permissive: 0 }, graded_weeks: 2 } });
  const order = sortRows([none, zero, row()], (r) => sortValue(r, "permissive"), "desc").map((r) => r.summary.graded_weeks);
  assert.deepEqual(order, [1, 2, 0]);
  assert.deepEqual(sortRows([none, zero, row()], (r) => sortValue(r, "permissive"), "asc").map((r) => r.summary.graded_weeks), [2, 1, 0]);
});

test("without a viewer team the analyzer link is neutral, never 'Trade away'", () => {
  assert.deepEqual(rowLinks(row(), "RB", null).trade, { href: "/trades?ta=1&a=201", label: "Trade analyzer" });
});

test("the grid names which positions share its ranking; WR/TE context is said to be context only", () => {
  const meta = { position: "WR" as const, metric: { ...META.metric, positions: ["QB", "WR", "TE"] as ("QB" | "WR" | "TE")[] } };
  assert.equal(metricScope(meta), "the same ranking for QB, WR and TE");
  assert.equal(metricScope({ position: "RB", metric: { ...META.metric, positions: ["RB"] } }), "RB only");
  const ctx = { ...graded, context: { metric: "wr_target_share", label: "Share of targets to WRs", value: 0.5, numerator: 120, denominator: 240, missing_reason: null } };
  assert.equal(contextText(ctx), " Context only, not part of the grade: Share of targets to WRs 50.0% (120 of 240 targets).");
  assert.match(describeCell(ctx, META), /Sample: 8 presumed-complete games, 320 plays\. Context only, not part of the grade/);
  assert.equal(contextText(graded), "");
  assert.match(contextText({ ...graded, context: { ...ctx.context, value: null, numerator: null, denominator: null, missing_reason: "no targets" } }), /unavailable \(no targets\)/);
});
