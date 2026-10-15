import assert from "node:assert/strict";
import { test } from "node:test";
import { initials, playerPhoto } from "./player-photo.ts";

test("playerPhoto: a player id gets their ESPN headshot at the requested size", () => {
  const p = playerPhoto(3918298, 48);
  assert.equal(p?.kind, "headshot");
  assert.match(p!.src, /^https:\/\/a\.espncdn\.com\/combiner\/i\?img=\/i\/headshots\/nfl\/players\/full\/3918298\.png&w=48&h=48/);
});

test("playerPhoto: a D/ST id gets its team logo", () => {
  assert.deepEqual(playerPhoto(-16012, 40)?.kind, "logo");
  assert.match(playerPhoto(-16012, 40)!.src, /teamlogos\/nfl\/500\/kc\.png/);
  assert.match(playerPhoto(-16028, 40)!.src, /teamlogos\/nfl\/500\/wsh\.png/);
  assert.match(playerPhoto(-16033, 40)!.src, /teamlogos\/nfl\/500\/bal\.png/);
});

test("playerPhoto: ids with no image return null", () => {
  assert.equal(playerPhoto(-16031, 40), null);          // no such NFL team
  assert.equal(playerPhoto(-5, 40), null);
  assert.equal(playerPhoto(0, 40), null);
  assert.equal(playerPhoto(10 ** 12 + 2, 40), null);    // K/D/ST streamer stand-in
  assert.equal(playerPhoto(1.5, 40), null);
});

test("initials: first and last word, punctuation and suffixes dropped", () => {
  assert.equal(initials("Ja'Marr Chase"), "JC");
  assert.equal(initials("Marvin Harrison Jr."), "MH");
  assert.equal(initials("Amon-Ra St. Brown"), "AB");
  assert.equal(initials("Chiefs D/ST"), "CD");
  assert.equal(initials("Puka"), "P");
  assert.equal(initials(null), "");
  assert.equal(initials("  "), "");
});
