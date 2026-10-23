import assert from "node:assert/strict";
import test from "node:test";
import { POSITIONS, RECS, VIEWS } from "../app/waivers/helpers.ts";
import { TEAM_SPECIFIC_PARAMS } from "./team-perspective.ts";
import {
  MAX_NAME, MAX_VIEWS, WAIVER_POSITIONS, WAIVER_RECS, WAIVER_VIEWS, cleanViewName, parseSavedViews, readSavedViews, removeView,
  sameView, saveView, savedViewsKey, serializeSavedViews, validViewParams, viewHref, viewParamsFromUrl, viewSummary, viewTool,
  writeSavedViews, type SavedView, type ViewParams,
} from "./saved-views.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const B = "lg_bbbbbbbbbbbbbbbb";

function memory() {
  const map = new Map<string, string>();
  return {
    map,
    io: {
      getItem: (k: string) => map.get(k) ?? null,
      setItem: (k: string, v: string) => { map.set(k, v); },
      removeItem: (k: string) => { map.delete(k); },
    },
  };
}

test("the waiver whitelist matches the Waivers page's own choices", () => {
  assert.deepEqual([...WAIVER_POSITIONS], POSITIONS.filter((p) => p !== "ALL"));
  assert.deepEqual([...WAIVER_RECS], RECS.filter((r) => r !== "ALL"));
  assert.deepEqual([...WAIVER_VIEWS].sort(), Object.keys(VIEWS).sort());
});

test("no view parameter depends on the analyzed team, so views need no team in their key", () => {
  for (const tool of ["rankings", "waivers", "discover"] as const) {
    for (const k of viewTool(tool).keys) assert.ok(!TEAM_SPECIFIC_PARAMS.includes(k) && k !== "team", `${tool}: ${k}`);
  }
  assert.deepEqual(viewParamsFromUrl("discover", new URLSearchParams("opponent=3&mode=fair")), [["mode", "fair"]]);
});

test("views are keyed by canonical league and tool; nothing is keyed outside a league", () => {
  assert.equal(savedViewsKey(A, "rankings"), "fi:saved-views:v1:lg_aaaaaaaaaaaaaaaa:rankings");
  assert.notEqual(savedViewsKey(A, "waivers"), savedViewsKey(B, "waivers"));
  assert.notEqual(savedViewsKey(A, "waivers"), savedViewsKey(A, "discover"));
  for (const lg of [null, undefined, "", "12345", "lg_short", "lg_aaaaaaaaaaaaaaaa/../x"]) assert.equal(savedViewsKey(lg, "rankings"), null);
  assert.equal(savedViewsKey(A, "players" as never), null);
});

test("capturing the URL keeps only whitelisted filters: team, offset, add/drop and bad values are left out", () => {
  const sp = new URLSearchParams("team=2&offset=100&pos=RB&pos=WR&horizon=ros&status=available&q=+Jos%C3%A9+&sort=xfp&dir=desc&add=1&league=clear&source=bogus");
  assert.deepEqual(viewParamsFromUrl("rankings", sp), [
    ["pos", "RB"], ["horizon", "ros"], ["status", "available"], ["q", "José"], ["sort", "xfp"], ["dir", "desc"],
  ]);
  // "All" is the page default, not a filter.
  assert.deepEqual(viewParamsFromUrl("rankings", new URLSearchParams("pos=ALL&status=all")), []);
  assert.deepEqual(viewParamsFromUrl("waivers", new URLSearchParams("view=CLAIMS&pos=D/ST&rec=claim&rec=WAIT&sort=owned&team=3&week=9")),
    [["view", "CLAIMS"], ["pos", "D/ST"], ["rec", "WAIT"], ["sort", "owned"]]);
  // Repeated shapes keep their order, without duplicates; the analyzed team never enters a view.
  assert.deepEqual(viewParamsFromUrl("discover", new URLSearchParams("team=4&mode=fair&shape=2-for-1&shape=1-for-1&shape=2-for-1&limit=99&target=307")),
    [["mode", "fair"], ["shape", "2-for-1"], ["shape", "1-for-1"], ["target", "307"]]);
});

test("a stored view's params are validated strictly: any unknown key or bad value refuses the whole view", () => {
  assert.deepEqual(validViewParams("rankings", [["pos", "RB"], ["horizon", "ros"]]), [["pos", "RB"], ["horizon", "ros"]]);
  assert.deepEqual(validViewParams("rankings", []), []);
  for (const bad of [
    "pos=RB", null, {}, [["pos"]], [["pos", 1]], [["team", "2"]], [["pos", "K"]], [["pos", "RB"], ["pos", "WR"]],
    [["q", " padded "]], [["q", "x".repeat(61)]], [["q", "a\u0000b"]], [["offset", "100"]], [["__proto__", "x"]],
    [["constructor", "x"]],
  ]) assert.equal(validViewParams("rankings", bad), null, JSON.stringify(bad));
  assert.equal(validViewParams("discover", [["shape", "1-for-1"], ["shape", "1-for-1"]]), null);
  assert.equal(validViewParams("discover", [["limit", "0"]]), null);
  assert.equal(validViewParams("discover", [["opponent", "3"]]), null);
  assert.equal(validViewParams("waivers", [["view", "best"]]), null);
});

test("applying a view builds the page href from validated params only", () => {
  assert.equal(viewHref("rankings", [["pos", "RB"], ["q", "Ja'Marr & co"]]), "/rankings?pos=RB&q=Ja%27Marr+%26+co");
  assert.equal(viewHref("waivers", [["view", "ALL"], ["rec", "CLAIM"]]), "/waivers?view=ALL&rec=CLAIM");
  assert.equal(viewHref("discover", [["mode", "fair"], ["shape", "1-for-1"], ["shape", "2-for-1"]]), "/trades/discover?mode=fair&shape=1-for-1&shape=2-for-1");
  // A tampered view never adds a key: it opens the page's default.
  assert.equal(viewHref("waivers", [["view", "ALL"], ["team", "9"]]), "/waivers");
  assert.equal(viewHref("rankings", "pos=RB&next=//evil.example"), "/rankings");
  for (const tool of ["rankings", "waivers", "discover"] as const)
    for (const p of viewTool(tool).presets) assert.ok(validViewParams(tool, p.params)?.length, `${tool} preset ${p.name}`);
});

test("views compare by filters, not order; summaries say the filters in words", () => {
  assert.equal(sameView([["pos", "RB"], ["horizon", "ros"]], [["horizon", "ros"], ["pos", "RB"]]), true);
  assert.equal(sameView([["pos", "RB"]], [["pos", "RB"], ["horizon", "ros"]]), false);
  assert.equal(viewSummary("rankings", [["pos", "RB"], ["horizon", "ros"], ["status", "available"], ["sort", "xfp"], ["dir", "desc"]]),
    "RB · Rest of season · Available · sorted by xFP/g, descending");
  assert.equal(viewSummary("waivers", [["view", "ALL"], ["rec", "CLAIM"]]), "All evaluated · Claim only");
  assert.equal(viewSummary("discover", [["mode", "fair"], ["shape", "1-for-1"]]), "Fairer asset exchanges · 1-for-1");
  assert.equal(viewSummary("rankings", []), "Default view");
});

test("names are trimmed, bounded and unique; saving the same name replaces that view in place", () => {
  assert.equal(cleanViewName("  RB   streamers "), "RB streamers");
  assert.equal(cleanViewName(""), null);
  assert.equal(cleanViewName("x".repeat(MAX_NAME + 1)), null);
  assert.equal(cleanViewName("é".repeat(MAX_NAME)), "é".repeat(MAX_NAME));
  assert.equal(cleanViewName("tab\u0007bell"), null);

  let views: SavedView[] = [];
  const first = saveView("rankings", views, "RB ros", [["pos", "RB"], ["horizon", "ros"]]);
  assert.ok(first.ok);
  views = first.views;
  const second = saveView("rankings", views, "Available", [["status", "available"]]);
  assert.ok(second.ok);
  views = second.views;
  const again = saveView("rankings", views, "rb ROS", [["pos", "WR"]]);
  assert.ok(again.ok && again.replaced);
  assert.deepEqual(again.views.map((v) => v.name), ["rb ROS", "Available"]);
  assert.deepEqual(again.views[0].params, [["pos", "WR"]]);
  assert.deepEqual(removeView(again.views, "Available").map((v) => v.name), ["rb ROS"]);

  assert.deepEqual(saveView("rankings", views, "   ", [["pos", "RB"]]), { ok: false, error: `Name the view (up to ${MAX_NAME} characters).` });
  assert.equal(saveView("rankings", views, "Nothing", []).ok, false);
  assert.equal(saveView("rankings", views, "Tampered", [["team", "2"]] as ViewParams).ok, false);
  const full = Array.from({ length: MAX_VIEWS }, (_, i) => ({ name: `View ${i}`, params: [["pos", "RB"]] as ViewParams }));
  assert.equal(saveView("rankings", full, "One more", [["pos", "QB"]]).ok, false);
  assert.equal(saveView("rankings", full, "view 3", [["pos", "QB"]]).ok, true);
});

test("views survive a reload in the same league and are never seen by another league, even when copied", () => {
  const { io, map } = memory();
  const views: SavedView[] = [{ name: "RB ros", params: [["pos", "RB"], ["horizon", "ros"]] }];
  assert.equal(writeSavedViews(io, A, "rankings", views), true);
  assert.deepEqual(readSavedViews(io, A, "rankings"), { views, available: true });
  assert.deepEqual(readSavedViews(io, B, "rankings").views, []);
  assert.deepEqual(readSavedViews(io, A, "waivers").views, []);
  // League A's payload pasted under league B's key (or another tool's) names its own league and tool: refused.
  map.set(savedViewsKey(B, "rankings")!, map.get(savedViewsKey(A, "rankings")!)!);
  assert.deepEqual(readSavedViews(io, B, "rankings").views, []);
  map.set(savedViewsKey(A, "waivers")!, map.get(savedViewsKey(A, "rankings")!)!);
  assert.deepEqual(readSavedViews(io, A, "waivers").views, []);
  // Outside a league nothing is read or written.
  assert.equal(writeSavedViews(io, null, "rankings", views), false);
  assert.deepEqual(readSavedViews(io, null, "rankings"), { views: [], available: false });
  // Deleting the last view removes the key.
  assert.equal(writeSavedViews(io, A, "rankings", []), true);
  assert.equal(map.has(savedViewsKey(A, "rankings")!), false);
});

test("malformed stored views are refused whole, and a view never stores data", () => {
  const ok = (views: unknown, extra: Record<string, unknown> = {}) => JSON.stringify({ v: 1, league: A, tool: "waivers", views, ...extra });
  for (const raw of [
    "{", "null", "[]", "42", ok("x"), ok([null]), ok([[]]), ok([{ name: "", params: [["view", "ALL"]] }]),
    ok([{ name: " padded", params: [["view", "ALL"]] }]), ok([{ name: "Empty", params: [] }]),
    ok([{ name: "Bad", params: [["view", "ALL"], ["team", "3"]] }]), ok([{ name: "Bad", params: "view=ALL" }]),
    ok([{ name: "Dup", params: [["view", "ALL"]] }, { name: "dup", params: [["pos", "RB"]] }]),
    ok(Array.from({ length: MAX_VIEWS + 1 }, (_, i) => ({ name: `V${i}`, params: [["view", "ALL"]] }))),
    JSON.stringify({ v: 2, league: A, tool: "waivers", views: [] }), ok([], { league: B }), ok([], { tool: "rankings" }),
  ]) assert.deepEqual(parseSavedViews(raw, A, "waivers"), [], raw);
  // Extra fields on a view (rows, players, points) are not carried: only name and params come back.
  const parsed = parseSavedViews(ok([{ name: "Claims", params: [["view", "ALL"], ["rec", "CLAIM"]], players: [{ id: 1 }] }]), A, "waivers");
  assert.deepEqual(parsed, [{ name: "Claims", params: [["view", "ALL"], ["rec", "CLAIM"]] }]);
  assert.deepEqual(Object.keys(JSON.parse(serializeSavedViews(A, "waivers", parsed))).sort(), ["league", "tool", "v", "views"]);
});

test("unavailable storage fails safely and an unscoped read never touches storage", () => {
  let reads = 0;
  const io = {
    getItem: () => { reads++; throw new Error("denied"); },
    setItem: () => { throw new Error("quota"); },
    removeItem: () => { throw new Error("denied"); },
  };
  assert.deepEqual(readSavedViews(io, A, "rankings"), { views: [], available: false });
  assert.equal(writeSavedViews(io, A, "rankings", [{ name: "x", params: [["pos", "RB"]] }]), false);
  assert.deepEqual(readSavedViews(io, null, "rankings"), { views: [], available: false });
  assert.equal(reads, 1);
});
