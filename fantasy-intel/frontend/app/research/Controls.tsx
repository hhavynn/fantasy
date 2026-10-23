import Link from "@/components/LeagueLink";
import { Method } from "@/components/ui";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { VOLUME_OPTIONS, gamesOptions, researchHref, WINDOWS, type ResearchState, type Slug } from "@/lib/research";
import type { OpportunityResponse, ResearchPosition } from "@/lib/research-types";
import styles from "./research.module.css";

/** Pool filters as a plain GET form: works without JavaScript, and the result is a shareable URL. On a league page it
 *  stays in that league (`/l/<lg>/research/<slug>`). */
export async function Filters({ slug, r, state }: { slug: Slug; r: OpportunityResponse; state: ResearchState }) {
  const f = r.filters;
  const d = r.defaults;
  const action = withLeague(`/research/${slug}`, (await currentLeague())?.id);
  const keep = { view: state.view !== "opportunity" ? state.view : null, y: state.y !== "fpoe" ? state.y : null, sel: state.sel, cmp: state.cmp };
  return (
    <form className={styles.filters} action={action} method="get" aria-label="Player pool filters">
      {Object.entries(keep).map(([k, v]) => (v ? <input key={k} type="hidden" name={k} value={v} /> : null))}
      <label className={styles.field}>
        <span>Games</span>
        <select name="window" defaultValue={f.window}>
          {WINDOWS.map((w) => <option key={w} value={w}>{r.windows[w]}</option>)}
        </select>
      </label>
      <label className={styles.field}>
        <span>Min. qualifying games</span>
        <select name="games" defaultValue={state.minGames === null ? "" : String(state.minGames)}>
          <option value="">Default ({Math.min(d.min_games, gamesOptions(f.window).length)})</option>
          {gamesOptions(f.window).map((g) => <option key={g} value={g}>{g}+</option>)}
        </select>
      </label>
      <label className={styles.field}>
        <span>Min. {f.volume_label}</span>
        <select name="vol" defaultValue={state.minVolume === null ? "" : String(state.minVolume)}>
          <option value="">Default ({d.min_volume})</option>
          {VOLUME_OPTIONS[f.position].map((v) => <option key={v} value={v}>{v === 0 ? "Any" : `${v}+`}</option>)}
        </select>
      </label>
      <label className={styles.field}>
        <span>Team</span>
        <select name="team" defaultValue={f.team ?? ""}>
          <option value="">All teams</option>
          {r.teams.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      <div className={styles.filterActions}>
        <button type="submit">Apply</button>
        <Link href={`/research/${slug}`}>Reset</Link>
      </div>
    </form>
  );
}

export function YToggle({ slug, state }: { slug: Slug; state: ResearchState }) {
  return (
    <nav className={styles.segmented} aria-label="Vertical axis">
      <Link href={researchHref(slug, state, { y: "fpoe" })} scroll={false} aria-current={state.y === "fpoe" ? "true" : undefined}>FPOE/G</Link>
      <Link href={researchHref(slug, state, { y: "ppr" })} scroll={false} aria-current={state.y === "ppr" ? "true" : undefined}>PPR/G</Link>
    </nav>
  );
}

const POSITION_NOTES: Record<ResearchPosition, string> = {
  WR: "Default pool: 2+ qualifying games and 4+ targets per game (a per-game average over the window). The reference video used 7+ targets and 2+ games; set the target filter to 7+ to match it.",
  RB: "Default pool: 2+ qualifying games and 8+ carries plus targets per game. That keeps committee backs and drops pure change-of-pace or special-teams backs. Backfield share is designed runs by running backs only; team rush share counts every rush, quarterback scrambles included.",
  TE: "Default pool: 2+ qualifying games and 3+ targets per game. Tight ends see fewer targets than receivers, and a blocking tight end with one target a game has no fantasy question to answer. Snap share is not route participation.",
};

/** Definitions and method, collapsed by default (decision first, evidence second, method last). */
export function ResearchMethod({ r }: { r: OpportunityResponse }) {
  const pos = r.filters.position;
  const routes = (r.data?.route_rows ?? 0) > 0;
  return (
    <Method title="Definitions and method">
      <p><b>xFP (expected fantasy points)</b>: what an average player would score in full PPR from the same targets and carries, given where on the field and how far downfield they came (nflverse ffopportunity). It measures the role, not the player.</p>
      <p><b>FPOE (fantasy points over expectation)</b>: actual nflverse PPR points minus xFP. FPOE/G is the mean of (PPR − xFP) over the same games used for xFP/G, so the two averages never mix different weeks.</p>
      <p><b>Qualifying game</b>: a game the player played (an offensive snap, target or carry) with both nflverse xFP and PPR points. A played game whose xFP is not yet published is left out of every average and listed, never counted as zero. Byes and missed games are skipped.</p>
      <p><b>Windows</b>: Season uses every qualifying game; Last 3 and Last 5 use the player&apos;s own three or five most recent qualifying games (not calendar weeks). Momentum compares the last 3 qualifying games (2 when the player has 4–5) with up to 5 before them, over the season, using the same trend windows as the player profile.</p>
      <p><b>Shares</b> are totals over the window: target share = targets ÷ team targets, air-yard share = air yards ÷ team air yards, backfield share = designed runs ÷ team running-back designed runs. <b>WOPR</b> = 1.5 × target share + 0.7 × air-yard share (the nflverse weighted opportunity rating). Snap share is the average of weekly offensive snap shares. Per-game counts divide by the games the source covered; partial coverage is flagged.</p>
      <p><b>Groups</b>: high opportunity means xFP/G at or above {r.high_opportunity_xfp} for a {pos} (the existing high tier). Production is above or below xFP when the gap is at least max({r.value_gap_margin.abs} points, {Math.round(r.value_gap_margin.rel * 100)}% of xFP) per game over 3+ games, the same margin the player scores use. Groups and the three research lists are descriptive starting points, not buy or sell calls; strong players can beat an average-player model for a long time.</p>
      <p><b>Confidence badges</b> count games behind a momentum read (high: 3 recent vs 4+ earlier; medium: 3 vs 3; low: 2 recent). They describe sample coverage, not statistical certainty.</p>
      <p><b>Scoring basis</b>: {r.data?.scoring_basis ?? "nflverse full PPR"}. Your league&apos;s custom scoring is not applied here.</p>
      <p>{POSITION_NOTES[pos]}</p>
      <p><b>Not shown</b>: routes run, route participation, targets and yards per route run, and alignment splits {routes ? "come only from an authorized external import, shown on the player profile" : "are unavailable in-season from free data and are never estimated from snap counts"}. FTN read-progression codes are unverified and not used. No ADP, trade values or acceptance odds are shown.</p>
    </Method>
  );
}
