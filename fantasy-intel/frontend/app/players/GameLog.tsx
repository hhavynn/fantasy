import type { ReactNode } from "react";
import { providerName } from "@/lib/labels";
import { ESPN_PROFILE, type LeagueProfile } from "@/lib/league-profile";
import { EmptyState, Method, Section, Tabs } from "@/components/ui";
import { int, num, pct } from "@/lib/format";
import { BOX_SCORE_NOTE, boxScoreWeeks, inRange, leagueLogRows, type WeekRange } from "@/lib/player-tabs";
import { FTN_READ1_NOTE } from "@/lib/types";
import type { UsageWeek } from "@/lib/types";
import styles from "./players.module.css";
import type { Detail } from "./types";

type Tier = "core" | "more";
type Col = {
  key: string;
  label: string;
  title?: string;
  tier: Tier;
  /** Hide the column for positions where it carries no meaning. */
  when?: (pos: string | null) => boolean;
  faint?: boolean;
  cell: (w: UsageWeek) => ReactNode;
};

const rushes = (pos: string | null) => pos === "RB" || pos === "QB";
const notRB = (pos: string | null) => pos !== "RB";

/**
 * Every column the old table showed. "Core" is the scannable default; the rest sit behind the
 * "All columns" tab. For a QB the receiving columns are rarely meaningful, so they move to "more".
 */
/** A league-points cell Fantasy Intel scored from the NFL box score (#254 S3). */
function BoxMark() {
  return <span className="faint" title={BOX_SCORE_NOTE}> †</span>;
}

function columns(pos: string | null, provider = "ESPN", box: ReadonlySet<number> = new Set()): Col[] {
  const receiving: Tier = pos === "QB" ? "more" : "core";
  const all: Col[] = [
    { key: "snaps", label: "Snaps", tier: "more", cell: (w) => int(w.offense_snaps, "No snap record") },
    { key: "snap_pct", label: "Snap%", tier: "core", cell: (w) => pct(w.offense_pct, 0, "No snap record") },
    { key: "carries", label: "Car", tier: "core", when: rushes, cell: (w) => int(w.carries) },
    { key: "team_rush", label: "Team rush sh", title: "Rush attempts (incl. scrambles, excl. kneels) / team rush attempts", tier: "core", when: rushes, cell: (w) => pct(w.team_rush_share, 0, "No play-by-play") },
    { key: "backfield", label: "Backfield sh", title: "Designed rushes / team RB designed rushes", tier: "core", when: (p) => p === "RB", cell: (w) => pct(w.backfield_rush_share, 0, "No RB carries for team") },
    { key: "rush_yd", label: "Rush yd", tier: "core", when: rushes, cell: (w) => int(w.rushing_yards) },
    { key: "ypc", label: "YPC", tier: "more", when: rushes, cell: (w) => num(w.yards_per_carry, 1, "No carries") },
    { key: "succ", label: "Succ%", title: "Designed rushes with EPA > 0", tier: "more", when: rushes, cell: (w) => pct(w.rushing_success_rate, 0, "No designed rushes") },
    { key: "ybc", label: "YBC/a", title: "PFR yards before contact / att", tier: "more", when: rushes, cell: (w) => num(w.ybc_per_att, 1, "No PFR row") },
    { key: "yac", label: "YAC/a", title: "PFR yards after contact / att", tier: "more", when: rushes, cell: (w) => num(w.yac_per_att, 1, "No PFR row") },
    { key: "bt", label: "BT", title: "PFR broken tackles (rush + rec)", tier: "more", when: rushes, cell: (w) => int(w.broken_tackles, "No PFR row") },
    { key: "tgt", label: "Tgt", tier: receiving, cell: (w) => int(w.targets) },
    { key: "tgt_sh", label: "Tgt sh", tier: receiving, cell: (w) => pct(w.target_share) },
    { key: "rec", label: "Rec", tier: receiving, cell: (w) => int(w.receptions) },
    { key: "rec_yd", label: "Rec yd", tier: receiving, cell: (w) => int(w.receiving_yards) },
    { key: "air", label: "Air yd", tier: "more", when: notRB, cell: (w) => int(w.air_yards) },
    { key: "adot", label: "aDOT", tier: "more", when: notRB, cell: (w) => num(w.adot, 1, "No targets") },
    { key: "wopr", label: "WOPR", tier: "more", when: notRB, cell: (w) => num(w.wopr, 2) },
    { key: "ftn", label: "FTN code-1 sh*", title: FTN_READ1_NOTE, tier: "more", when: notRB, faint: true, cell: (w) => pct(w.ftn_read1_target_share, 0, "Game not charted by FTN") },
    { key: "rz_tgt", label: "RZ tgt", tier: "more", cell: (w) => int(w.rz_targets, "No play-by-play") },
    { key: "rz_car", label: "RZ car", tier: "more", cell: (w) => int(w.rz_carries, "No play-by-play") },
    { key: "gl_car", label: "GL car", tier: "more", cell: (w) => int(w.gl_carries, "No play-by-play") },
    { key: "td", label: "TD", tier: "core", cell: (w) => int(w.receiving_tds == null && w.rushing_tds == null ? null : (w.receiving_tds ?? 0) + (w.rushing_tds ?? 0), "No touchdown record") },
    { key: "ppr", label: "PPR", tier: "core", cell: (w) => num(w.fantasy_points_ppr) },
    { key: "league", label: "League pts", tier: "core", cell: (w) => <>{num(w.league_points, 1, `No ${provider} weekly points`)}{box.has(w.week) && <BoxMark />}</> },
    { key: "xfp", label: "xFP", tier: "core", cell: (w) => num(w.xfp, 1, "Not in nflverse ff_opportunity") },
    { key: "source", label: "Source", tier: "more", cell: (w) => <span className={styles.source}>{w.stats_source}</span> },
  ];
  return all.filter((c) => !c.when || c.when(pos));
}

/**
 * Week-by-week table. `all` shows every column; the default shows the core set. `colsHref` links the two column sets
 * (relative hrefs that keep the rest of the profile URL); `range` narrows the rows to a week window.
 */
export function GameLog({ d, all, colsHref, range = null, profile = ESPN_PROFILE }: {
  d: Detail; all: boolean; colsHref: { core: string; all: string }; range?: WeekRange | null; profile?: LeagueProfile;
}) {
  const provider = providerName(profile.provider);
  const pos = d.player.position;
  const weeks = d.weekly.filter((w) => inRange(w.week, range));
  const route = d.route_data;
  const box = boxScoreWeeks(d.league_points);

  const notes = (
    <Method title="Data notes">
      <div className={styles.dataNotes}>
        <p>
          {route
            ? <>Route data: external provider <b>{route.providers.join(", ")}</b> (not nflverse), weeks {route.weeks.join(", ")}.</>
            : <>Route participation / routes run: <b>unavailable</b> ({d.unavailable_metrics.route_participation}). Snap share is shown instead.</>}
        </p>
        <p>
          * FTN code-1 share is experimental: read-code semantics are unresolved upstream (nflverse/nflreadr#325); it is not used in any score.
        </p>
        <p>“—” = data missing (hover for the reason); 0 = measured zero.</p>
        {box.size > 0 && <p>† League points for week {[...box].sort((a, b) => a - b).join(", ")}: {BOX_SCORE_NOTE}</p>}
      </div>
    </Method>
  );

  if (d.weekly.length === 0) return <LeagueLog d={d} notes={notes} provider={provider} espnProjections={profile.providerProjections} range={range} />;
  if (weeks.length === 0) {
    return (
      <Section title="Game log">
        <EmptyState title="No games recorded in these weeks">
          No NFL usage is stored for weeks {range?.from}–{range?.to}. A week without a record is unknown, not zero; widen the window to see earlier games.
        </EmptyState>
        <div className={styles.logNotesBare}>{notes}</div>
      </Section>
    );
  }

  const cols = columns(pos, provider, box).filter((c) => all || c.tier === "core");
  return (
    <Section
      title="Game log"
      actions={
        <Tabs
          label="Game log columns"
          current={all ? colsHref.all : colsHref.core}
          items={[{ href: colsHref.core, label: "Core columns" }, { href: colsHref.all, label: "All columns" }]}
        />
      }
      flush
    >
      <div className="table-wrap">
        <table className={styles.log}>
          <thead>
            <tr>
              <th scope="col">Wk</th>
              {cols.map((c) => <th key={c.key} scope="col" className="num" title={c.title}>{c.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {weeks.map((w) => (
              <tr key={w.week}>
                <td><span className={styles.week}>{w.week}</span> <span className={styles.opp}>{w.opponent ?? ""}</span></td>
                {cols.map((c) => <td key={c.key} className={c.faint ? "num faint" : "num"} title={c.faint ? FTN_READ1_NOTE : undefined}>{c.cell(w)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={styles.logNotes}>{notes}</div>
    </Section>
  );
}

/** Kickers, defenses and unmatched players have no NFL usage rows: show what ESPN scored and projected (a Sleeper league:
 * what Sleeper scored; it publishes no projections). */
function LeagueLog({ d, notes, provider, espnProjections, range }: {
  d: Detail; notes: ReactNode; provider: string; espnProjections: boolean; range: WeekRange | null;
}) {
  const asOf = d.forward_projection?.summary.as_of_week ?? null;
  const rows = leagueLogRows(d.league_points, asOf, range);
  if (rows.length === 0) {
    return (
      <Section title="Game log">
        <EmptyState title="No games recorded">
          {range
            ? `No ${provider} weekly points are stored for weeks ${range.from}–${range.to}.`
            : `No NFL games or ${provider} weekly points are stored for this player this season.`}
        </EmptyState>
        <div className={styles.logNotesBare}>{notes}</div>
      </Section>
    );
  }
  return (
    <Section title="Game log" flush>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th scope="col">Wk</th><th scope="col" className="num">League pts</th>{espnProjections && <th scope="col" className="num">ESPN proj</th>}</tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.scoring_period}>
                <td><span className={styles.week}>{r.scoring_period}</span></td>
                <td className="num">{num(r.actual_points, 1, `Not played yet / no ${provider} points`)}{r.actual_source === "box_score" && <BoxMark />}</td>
                {espnProjections && <td className="num">{num(r.projected_points, 1, "ESPN returned no projection for this week (unknown, not 0)")}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={styles.logNotes}>
        <p>No NFL usage rows are stored for this player, so only {provider}&apos;s weekly points are shown.</p>
        {notes}
      </div>
    </Section>
  );
}
