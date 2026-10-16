/** ESPN proTeamId -> ESPN abbreviation (mirrors backend/app/espn/constants.py PRO_TEAM_MAP). */
const PRO_TEAMS: Record<number, string> = {
  1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
  8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
  15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
  22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH",
  29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
};

/** ESPN gives each D/ST a negative player id: -16000 - proTeamId (-16012 is the Chiefs). */
const DST_BASE = -16000;
/** Real ESPN player ids stay far below this; the K/D/ST streamer stand-ins sit at 10**12 + n. */
const MAX_REAL_ID = 10 ** 12;

export type PlayerPhotoSource = { kind: "headshot" | "logo"; src: string };

/** ESPN's resizing proxy, so a 24px avatar doesn't download the ~1000px original. */
function combiner(path: string, px: number): string {
  return `https://a.espncdn.com/combiner/i?img=${path}&w=${px}&h=${px}&scale=crop&cquality=80`;
}

/** The ESPN image for a player id, fetched at `px` pixels square (ask for 2x the rendered size):
 * the headshot for a player, the team logo for a D/ST. Null when the id has no image (streamer stand-ins, unknown ids).
 * A player with no ESPN headshot still gets a URL; the component falls back to initials when it 404s. */
export function playerPhoto(id: number, px: number): PlayerPhotoSource | null {
  if (!Number.isInteger(id) || id === 0 || id >= MAX_REAL_ID) return null;
  if (id < 0) {
    const team = PRO_TEAMS[DST_BASE - id];
    return team ? { kind: "logo", src: combiner(`/i/teamlogos/nfl/500/${team.toLowerCase()}.png`, px) } : null;
  }
  return { kind: "headshot", src: combiner(`/i/headshots/nfl/players/full/${id}.png`, px) };
}

const SUFFIXES = new Set(["JR", "SR", "II", "III", "IV", "V"]);

/** Up to two initials for the no-photo fallback: "Ja'Marr Chase" -> "JC", "Marvin Harrison Jr." -> "MH". */
export function initials(name: string | null): string {
  if (!name) return "";
  const words = name.replace(/[^\p{L}\s]/gu, "").split(/\s+/).filter((w) => w && !SUFFIXES.has(w.toUpperCase()));
  if (words.length === 0) return "";
  const first = words[0][0];
  const last = words.length > 1 ? words[words.length - 1][0] : "";
  return (first + last).toUpperCase();
}
