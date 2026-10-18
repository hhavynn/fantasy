import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PAGE_SIZE,
  RANK_POSITIONS,
  RANKING_SORT_KEYS,
  availabilityWarning,
  formatPoints,
  formatRating,
  opponentLabel,
  parseRankingsFilters,
  playerHref,
  rankLabel,
  rankingsApiPath,
  rankingsHref,
  rosterLabel,
  trendRankValue,
  unrankedLabel,
} from "./rankings.ts";

// The rows are the API's JSON (docs/player_rankings.md); the tests pass only the fields a helper reads.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const asRow = (x: unknown): any => x;

const query = (path: string) => new URLSearchParams(path.split("?")[1] ?? "");

// ----------------------------------------------------------------------------- constants

test("constants: positions, sort keys and page size follow the API contract", () => {
  assert.deepEqual([...RANK_POSITIONS], ["ALL", "QB", "RB", "WR", "TE"]);
  assert.deepEqual(
    [...RANKING_SORT_KEYS].sort(),
    ["consistency", "espn", "name", "opportunity", "ours", "ppr", "production", "rank", "rating", "trend", "xfp"],
  );
  assert.equal(PAGE_SIZE, 100);
});

// ----------------------------------------------------------------------------- correct 0-100 display

test("formatRating: one decimal on the 0-100 scale", () => {
  assert.equal(formatRating(87.5), "87.5");
  assert.equal(formatRating(100), "100.0");
  assert.equal(formatRating(0), "0.0");
  assert.equal(formatRating(72.34), "72.3");
  assert.equal(formatRating(5), "5.0");
});

test("formatRating: null, undefined and NaN are a dash, never 0.0", () => {
  assert.equal(formatRating(null), "—");
  assert.equal(formatRating(undefined), "—");
  assert.equal(formatRating(Number.NaN), "—");
});

test("formatRating: values outside 0-100 are clamped", () => {
  assert.equal(formatRating(104.2), "100.0");
  assert.equal(formatRating(-3), "0.0");
});

test("formatPoints: one decimal, a missing value is a dash and a real zero stays 0.0", () => {
  assert.equal(formatPoints(18.44), "18.4");
  assert.equal(formatPoints(7), "7.0");
  assert.equal(formatPoints(0), "0.0");
  assert.equal(formatPoints(null), "—");
  assert.equal(formatPoints(undefined), "—");
  assert.equal(formatPoints(Number.NaN), "—");
});

// ----------------------------------------------------------------------------- ties and unranked labels

test("rankLabel: the position label, with T- when tied", () => {
  const row = (tied: boolean) => asRow({
    rank: { overall: 3, position: 3, position_label: "WR3", tied },
    projection: { status: "ok" },
  });
  assert.equal(rankLabel(row(false)), "WR3");
  assert.equal(rankLabel(row(true)), "T-WR3");
});

test("rankLabel: an unranked player shows why instead of a rank", () => {
  const unranked = (status: string) => asRow({
    rank: { overall: null, position: null, position_label: null, tied: false },
    projection: { status },
  });
  assert.equal(rankLabel(unranked("bye")), "Bye");
  assert.equal(rankLabel(unranked("missing")), "No projection");
  assert.equal(rankLabel(unranked("incomplete")), "Incomplete");
  assert.equal(rankLabel(unranked("no_ours")), "No Ours estimate");
});

test("unrankedLabel: each reason and the fallback", () => {
  assert.equal(unrankedLabel("bye"), "Bye");
  assert.equal(unrankedLabel("missing"), "No projection");
  assert.equal(unrankedLabel("incomplete"), "Incomplete");
  assert.equal(unrankedLabel("no_ours"), "No Ours estimate");
  assert.equal(unrankedLabel("something_else"), "Unranked");
  assert.equal(unrankedLabel(null), "Unranked");
  assert.equal(unrankedLabel(undefined), "Unranked");
});

// ----------------------------------------------------------------------------- availability

const avail = (status: string, onBye: boolean | null = false) => asRow({
  availability: { status },
  game: { week: 7, opponent: "BUF", is_home: true, on_bye: onBye },
});

test("availabilityWarning: statuses map to labels", () => {
  assert.equal(availabilityWarning(avail("OUT")), "Out");
  assert.equal(availabilityWarning(avail("IR")), "Injured reserve");
  assert.equal(availabilityWarning(avail("SUSPENDED")), "Suspended");
  assert.equal(availabilityWarning(avail("DOUBTFUL")), "Doubtful");
  assert.equal(availabilityWarning(avail("QUESTIONABLE")), "Questionable");
});

test("availabilityWarning: healthy and not on bye is no warning", () => {
  assert.equal(availabilityWarning(avail("AVAILABLE")), null);
  assert.equal(availabilityWarning(avail("AVAILABLE", null)), null);
});

test("availabilityWarning: the bye wins over an injury, and shows for a healthy player", () => {
  assert.equal(availabilityWarning(avail("AVAILABLE", true)), "Bye week");
  assert.equal(availabilityWarning(avail("OUT", true)), "Bye week");
  assert.equal(availabilityWarning(avail("QUESTIONABLE", true)), "Bye week");
});

// ----------------------------------------------------------------------------- roster / links / opponent / trend

test("rosterLabel: my team, another team, free agent, waivers, unknown", () => {
  assert.deepEqual(rosterLabel(asRow({ status: "ROSTERED", fantasy_team: "Gridiron Ghosts", is_mine: true })),
    { text: "My team", tone: "accent" });
  assert.deepEqual(rosterLabel(asRow({ status: "ROSTERED", fantasy_team: "Gridiron Ghosts", is_mine: false })),
    { text: "Gridiron Ghosts", tone: "muted" });
  assert.deepEqual(rosterLabel(asRow({ status: "ROSTERED", fantasy_team: null, is_mine: false })),
    { text: "Rostered", tone: "muted" });
  assert.deepEqual(rosterLabel(asRow({ status: "FREE_AGENT", fantasy_team: null, is_mine: false })),
    { text: "Free agent", tone: "good" });
  assert.deepEqual(rosterLabel(asRow({ status: "WAIVERS", fantasy_team: null, is_mine: false })),
    { text: "Waivers", tone: "warn" });
});

test("rosterLabel: league context not synced is Unknown, not a claim", () => {
  assert.deepEqual(rosterLabel(asRow({ status: "UNKNOWN", fantasy_team: null, is_mine: false })),
    { text: "Unknown", tone: "muted" });
});

test("playerHref opens the existing player profile by ESPN id", () => {
  assert.equal(playerHref(201), "/players?id=201");
  assert.equal(playerHref(4262921), "/players?id=4262921");
});

test("opponentLabel: home, away, bye and missing context", () => {
  assert.equal(opponentLabel(asRow({ opponent: "BUF", is_home: true, on_bye: false })), "vs BUF");
  assert.equal(opponentLabel(asRow({ opponent: "BUF", is_home: false, on_bye: false })), "@ BUF");
  assert.equal(opponentLabel(asRow({ opponent: null, is_home: null, on_bye: true })), "BYE");
  assert.equal(opponentLabel(asRow({ opponent: "BUF", is_home: true, on_bye: true })), "BYE");
  // a player without an NFL team: no opponent and no bye information
  assert.equal(opponentLabel(asRow({ opponent: null, is_home: null, on_bye: null })), "—");
});

test("trendRankValue: UP > FLAT > DOWN, anything else is unranked", () => {
  assert.equal(trendRankValue("UP"), 2);
  assert.equal(trendRankValue("FLAT"), 1);
  assert.equal(trendRankValue("DOWN"), 0);
  assert.equal(trendRankValue("INSUFFICIENT"), null);
  assert.equal(trendRankValue(null), null);
  assert.equal(trendRankValue(undefined), null);
});

// ----------------------------------------------------------------------------- URL state

test("parseRankingsFilters: no params gives the defaults", () => {
  const f = parseRankingsFilters({}, undefined);
  assert.equal(f.pos, "ALL");
  assert.equal(f.horizon, "week");
  assert.equal(f.source, "espn");
  assert.equal(f.status, "all");
  assert.equal(f.q, "");
  assert.equal(f.offset, 0);
  assert.deepEqual(f.sort, { key: "rank", dir: "asc" });
});

test("parseRankingsFilters: valid params are read", () => {
  const f = parseRankingsFilters(
    { pos: "WR", horizon: "ros", source: "ours", status: "available", q: "  chase ", sort: "rating", dir: "asc", offset: "200" },
    "espn",
  );
  assert.equal(f.pos, "WR");
  assert.equal(f.horizon, "ros");
  assert.equal(f.source, "ours");
  assert.equal(f.status, "available");
  assert.equal(f.q, "chase");
  assert.deepEqual(f.sort, { key: "rating", dir: "asc" });
  assert.equal(f.offset, 200);
});

test("parseRankingsFilters: invalid values fall back to the defaults", () => {
  const f = parseRankingsFilters(
    { pos: "K", horizon: "month", source: "vegas", status: "everyone", sort: "bogus", dir: "sideways", offset: "-5" },
    undefined,
  );
  assert.equal(f.pos, "ALL");
  assert.equal(f.horizon, "week");
  assert.equal(f.source, "espn");
  assert.equal(f.status, "all");
  assert.deepEqual(f.sort, { key: "rank", dir: "asc" });
  assert.equal(f.offset, 0);
  assert.equal(parseRankingsFilters({ offset: "abc" }, undefined).offset, 0);
});

test("parseRankingsFilters: a column sort without dir uses the column default (desc for numbers, asc for rank, name and consistency)", () => {
  assert.deepEqual(parseRankingsFilters({ sort: "rating" }, undefined).sort, { key: "rating", dir: "desc" });
  assert.deepEqual(parseRankingsFilters({ sort: "consistency" }, undefined).sort, { key: "consistency", dir: "asc" });
  assert.deepEqual(parseRankingsFilters({ sort: "name" }, undefined).sort.dir, "asc");
});

test("parseRankingsFilters: q is trimmed and capped at 60 characters", () => {
  assert.equal(parseRankingsFilters({ q: "   a b   " }, undefined).q, "a b");
  assert.equal(parseRankingsFilters({ q: "x".repeat(200) }, undefined).q.length, 60);
});

test("parseRankingsFilters: the URL source wins over the cookie, the cookie fills in when absent", () => {
  assert.equal(parseRankingsFilters({ source: "espn" }, "ours").source, "espn");
  assert.equal(parseRankingsFilters({ source: "ours" }, "espn").source, "ours");
  assert.equal(parseRankingsFilters({}, "ours").source, "ours");
  assert.equal(parseRankingsFilters({}, "espn").source, "espn");
  assert.equal(parseRankingsFilters({ source: "nonsense" }, "ours").source, "ours");
});

test("parseRankingsFilters: array-valued params use the first value", () => {
  assert.equal(parseRankingsFilters({ pos: ["TE", "QB"] }, undefined).pos, "TE");
});

test("rankingsApiPath: the defaults map to the API's own defaults", () => {
  const path = rankingsApiPath(parseRankingsFilters({}, undefined));
  assert.ok(path.startsWith("/api/rankings?"));
  const p = query(path);
  assert.equal(p.get("position"), "ALL");
  assert.equal(p.get("horizon"), "week");
  assert.equal(p.get("source"), "espn");
  assert.equal(p.get("sort"), "rank");
  assert.equal(p.get("dir"), "asc");
  assert.equal(p.get("status"), "all");
  assert.equal(p.get("limit"), String(PAGE_SIZE));
  assert.equal(p.get("offset"), "0");
  assert.equal(p.has("q"), false);
});

test("rankingsApiPath: Ours becomes source=independent and q is sent only when non-empty", () => {
  const f = parseRankingsFilters({ source: "ours", q: "Jefferson", pos: "WR", sort: "xfp", offset: "100" }, undefined);
  const p = query(rankingsApiPath(f, 50));
  assert.equal(p.get("source"), "independent");
  assert.equal(p.get("q"), "Jefferson");
  assert.equal(p.get("position"), "WR");
  assert.equal(p.get("sort"), "xfp");
  assert.equal(p.get("dir"), "desc");
  assert.equal(p.get("limit"), "50");
  assert.equal(p.get("offset"), "100");
});

test("rankingsApiPath: a name with spaces and punctuation is encoded", () => {
  const p = query(rankingsApiPath(parseRankingsFilters({ q: "St. Brown & co" }, undefined)));
  assert.equal(p.get("q"), "St. Brown & co");
});

test("URL round trip: parse -> API path carries every filter the URL held", () => {
  const sp = { pos: "RB", horizon: "ros", source: "ours", status: "rostered", q: "kamara", sort: "consistency", dir: "asc", offset: "300" };
  const f = parseRankingsFilters(sp, undefined);
  const p = query(rankingsApiPath(f));
  assert.deepEqual(
    {
      position: p.get("position"), horizon: p.get("horizon"), source: p.get("source"), status: p.get("status"),
      q: p.get("q"), sort: p.get("sort"), dir: p.get("dir"), offset: p.get("offset"),
    },
    { position: "RB", horizon: "ros", source: "independent", status: "rostered", q: "kamara", sort: "consistency", dir: "asc", offset: "300" },
  );
});

test("rankingsApiPath: every allowed sort key and position survives parsing", () => {
  for (const key of RANKING_SORT_KEYS) {
    assert.equal(query(rankingsApiPath(parseRankingsFilters({ sort: key }, undefined))).get("sort"), key);
  }
  for (const pos of RANK_POSITIONS) {
    assert.equal(query(rankingsApiPath(parseRankingsFilters({ pos }, undefined))).get("position"), pos);
  }
});

// ----------------------------------------------------------------------------- rankingsHref

const params = (href: string) => new URLSearchParams(href.replace(/^\?/, ""));

test("rankingsHref: sets a param and preserves the others", () => {
  const p = params(rankingsHref({ horizon: "ros", q: "chase" }, { pos: "WR" }));
  assert.equal(p.get("pos"), "WR");
  assert.equal(p.get("horizon"), "ros");
  assert.equal(p.get("q"), "chase");
});

test("rankingsHref: a null value deletes the param", () => {
  const p = params(rankingsHref({ pos: "WR", horizon: "ros" }, { pos: null }));
  assert.equal(p.has("pos"), false);
  assert.equal(p.get("horizon"), "ros");
});

test("rankingsHref: a filter change drops the page offset", () => {
  const sp = { pos: "WR", offset: "200" };
  assert.equal(params(rankingsHref(sp, { status: "available" })).has("offset"), false);
  assert.equal(params(rankingsHref(sp, { pos: "TE" })).has("offset"), false);
  assert.equal(params(rankingsHref(sp, { sort: "rating", dir: "desc" })).has("offset"), false);
  assert.equal(params(rankingsHref(sp, { pos: null })).has("offset"), false);
});

test("rankingsHref: paging alone keeps the filters and sets the offset", () => {
  const p = params(rankingsHref({ pos: "WR", status: "available", offset: "100" }, { offset: "200" }));
  assert.equal(p.get("offset"), "200");
  assert.equal(p.get("pos"), "WR");
  assert.equal(p.get("status"), "available");
});

test("rankingsHref: unrelated params are preserved and the result starts with ?", () => {
  const href = rankingsHref({ pos: "QB", utm: "x" }, { horizon: "ros" });
  assert.ok(href.startsWith("?"));
  assert.equal(params(href).get("utm"), "x");
});

test("rankingsHref -> parseRankingsFilters: the link we render parses back to the same view", () => {
  const sp = { pos: "WR", source: "ours", offset: "100" };
  const href = rankingsHref(sp, { horizon: "ros", status: "available" });
  const f = parseRankingsFilters(Object.fromEntries(params(href)), undefined);
  assert.equal(f.pos, "WR");
  assert.equal(f.source, "ours");
  assert.equal(f.horizon, "ros");
  assert.equal(f.status, "available");
  assert.equal(f.offset, 0);
});
