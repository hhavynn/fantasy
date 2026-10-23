import assert from "node:assert/strict";
import test from "node:test";
import { discoveryHref, discoveryLinks, ofText, seasonDisclosures, type CounterpartySeason } from "./trade-counterparty.ts";

const report = (isMine: boolean, sent: string[]) => ({
  team: { team_id: 4, name: "Team 4", owner_names: null, is_mine: isMine },
  discovery: { opponent_team_id: 4, positions_sent: sent },
});

test("discoveryHref uses trade discovery's own URL keys", () => {
  assert.equal(discoveryHref(4), "/trades/discover?mode=targeted&opponent=4");
  assert.equal(discoveryHref(4, "D/ST"), "/trades/discover?mode=targeted&opponent=4&pos=D%2FST");
});

test("discoveryLinks states the basis of every position and skips your own team", () => {
  const roster = [{ position: "WR", league_comparison: "ABOVE" }, { position: "IR", league_comparison: "ABOVE" },
    { position: "QB", league_comparison: "BELOW" }];
  const links = discoveryLinks(report(false, ["RB", "WR"]), roster);
  assert.deepEqual(links.map((l) => [l.position, l.basis]), [
    [null, "All positions"],
    ["RB", "sent in recorded trades"],
    ["WR", "sent in recorded trades; rostered above the league average now"],
  ]);
  assert.equal(links[1].href, "/trades/discover?mode=targeted&opponent=4&pos=RB");
  assert.deepEqual(discoveryLinks(report(true, ["RB"]), roster), []);
  // suppressed history (the backend sends no positions): only the roster basis remains
  assert.deepEqual(discoveryLinks(report(false, []), []).map((l) => l.position), [null]);
});

test("ofText and season disclosures", () => {
  assert.equal(ofText(2, 5), "2 of 5");
  const seasons: CounterpartySeason[] = [
    { season: 2026, status: "included", team_trades: 1, league_trades: 3, reason: null },
    { season: 2025, status: "excluded", team_trades: 2, league_trades: 4, reason: "another manager" },
  ];
  assert.deepEqual(seasonDisclosures(seasons).map((s) => s.season), [2025]);
});
