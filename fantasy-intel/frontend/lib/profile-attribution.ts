// "Why it changed" on the player profile (issue #225): Fantasy Intel's own forecast change for one player, read from the
// Projection movers route with `player=` (contract "projection-attribution/v1" on the mover). Exact match only: the
// movers page falls back to the biggest riser when the requested player is missing, a profile never shows another
// player's change. Anything that cannot be compared is said in words, never shown as a zero change.

import {
  CONTRACT, POSITIONS, moversApiQuery, moversHref, requestedMissingText, type Mover, type MoversResponse,
  type MoversState,
} from "./projection-movers.ts";

/** The movers list the profile asks for is one riser and one faller: the requested player is kept beyond the cap. */
export const PROFILE_LIMIT = 1;

export function profileMoversState(id: number): MoversState {
  return { source: "independent", view: "ros", position: null, baseline: null, player: id };
}

/** The API query for one player's Fantasy Intel change (rest of season, default comparison point). */
export function profileMoversQuery(id: number): string {
  return moversApiQuery(profileMoversState(id), PROFILE_LIMIT);
}

/** Fantasy Intel forecasts (and so the movers) cover QB, RB, WR and TE only. */
export function attributionEligible(position: string | null | undefined): boolean {
  return (POSITIONS as readonly string[]).includes(position ?? "");
}

export type ProfileChange =
  | { kind: "mover"; mover: Mover; href: string }
  | { kind: "unavailable"; text: string; href: string };

/** What the profile shows for player `id` given the movers response (null: the request failed). */
export function profileChange(d: MoversResponse | null, id: number): ProfileChange {
  const state = profileMoversState(id);
  const listHref = moversHref(state);
  if (d === null) return { kind: "unavailable", text: "Fantasy Intel's forecast change could not be read right now.", href: listHref };
  if (d.contract !== CONTRACT) {
    return { kind: "unavailable", text: "The backend sent an unexpected format, so the forecast change is not shown.", href: listHref };
  }
  if (d.source !== "independent") {
    return { kind: "unavailable", text: "Only Fantasy Intel's own forecasts are split here.", href: listHref };
  }
  if (!d.available) {
    return { kind: "unavailable", text: d.reason ?? "There are not enough stored Fantasy Intel forecasts to compare yet.", href: listHref };
  }
  const hit = [...d.risers, ...d.fallers, ...d.players].find((m) => m.espn_player_id === id);
  if (hit) return { kind: "mover", mover: hit, href: moversHref(state, { player: id }) };
  return {
    kind: "unavailable",
    text: requestedMissingText(d) ?? "This player has no Fantasy Intel forecast in either compared forecast.",
    href: listHref,
  };
}
