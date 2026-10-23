import Link from "@/components/LeagueLink";
import { Badge, Section } from "@/components/ui";
import { int, num, pct, when } from "@/lib/format";
import { FTN_READ1_LABEL, FTN_READ1_NOTE } from "@/lib/types";
import type { RouteData } from "@/lib/types";
import styles from "./players.module.css";
import type { Detail } from "./types";

const GOOD = ["STRONG", "HIGH", "GOOD", "FAVORABLE", "ABOVE_AVERAGE", "ABOVE_EXPECTED", "SUPPORTIVE", "UP"];
const BAD = ["LIMITED", "LOW", "POOR", "TOUGH", "BELOW_AVERAGE", "BELOW_EXPECTED", "DOWN"];
const tone = (v?: string) => (GOOD.includes(v ?? "") ? "good" : BAD.includes(v ?? "") ? "bad" : "");

/** Role, usage, offensive environment and the next opponent, in four ruled columns rather than four boxes. */
export function ContextSection({ d }: { d: Detail }) {
  const ctx = d.context!;
  const L = ctx.labels;
  const e = ctx.evidence;
  const pos = d.player.position;
  const tc = d.team_context;
  const od = d.opponent_defense;
  return (
    <Section
      title="Player context"
      actions={<span className="faint">last {e.games_recent} games (wk {e.recent_weeks.join(", ")})</span>}
      flush
    >
      <div className={styles.contextGrid}>
        <div className={styles.contextCol}>
          <h3 className={styles.contextTitle}>Role <Badge value={L.role} tone={tone(L.role)} /> <Badge value={L.role_trend} /></h3>
          <div className="kv">
            <span className="k">Snap share</span><span>{pct(e.snap_pct)}</span>
            <span className="k">Target share</span><span>{pct(e.target_share)}</span>
            {pos === "RB" && <>
              <span className="k">Backfield share</span><span>{pct(e.backfield_rush_share)}</span>
              <span className="k">Team rush share</span><span>{pct(e.team_rush_share)}</span>
            </>}
            {pos !== "RB" && <><span className="k">Air-yard share</span><span>{pct(e.air_yards_share)}</span></>}
          </div>
        </div>
        <div className={styles.contextCol}>
          <h3 className={styles.contextTitle}>
            {pos === "RB" ? "Rushing" : "Receiving"} usage <Badge value={L.scoring_opportunity} tone={tone(L.scoring_opportunity)} title="Scoring opportunity" />
          </h3>
          <div className="kv">
            {pos === "RB" ? <>
              <span className="k">RZ carries / g</span><span>{num(e.rz_carries_pg)}</span>
              <span className="k">GL carries</span><span>{int(e.gl_carries_recent)}</span>
              <span className="k">Efficiency</span><Badge value={L.efficiency} tone={tone(L.efficiency)} />
              <span className="k">YBC / YAC per att</span><span>{num(e.ybc_per_att)} / {num(e.yac_per_att)}</span>
              <span className="k">Success / explosive</span><span>{pct(e.rushing_success_rate)} / {pct(e.explosive_run_rate)}</span>
              <span className="k">Run blocking</span><Badge value={L.run_blocking} tone={tone(L.run_blocking)} />
              <span className="k">Designed rushes</span><span>{e.designed_rushes_season} <span className="faint">(season sample)</span></span>
            </> : <>
              <span className="k">RZ targets / g</span><span>{num(e.rz_targets_pg)}</span>
              <span className="k">aDOT / WOPR</span><span>{num(e.adot)} / {num(e.wopr, 2)}</span>
              <span className="k">Designed targets</span><span>{int(e.designed_targets, "Not charted")}</span>
              <span className="k">Efficiency</span><Badge value={L.efficiency} tone={tone(L.efficiency)} />
              <span className="k">NGS separation</span><span>{num(e.ngs_avg_separation, 1, "NGS: not qualified / unavailable")}</span>
            </>}
          </div>
        </div>
        <div className={styles.contextCol}>
          <h3 className={styles.contextTitle}>Offensive environment <Badge value={L.offensive_environment} tone={tone(L.offensive_environment)} /></h3>
          <div className="kv">
            <span className="k">Next implied total</span><span>{num(tc?.next_implied_total, 1, "No betting line / bye")}</span>
            <span className="k">PROE</span><span>{num(tc?.proe)} <span className="faint">pts</span></span>
            <span className="k">Neutral pass rate</span><span>{pct(tc?.neutral_pass_rate)}</span>
            <span className="k">Sack / hit-or-sack</span><span>{pct(tc?.sack_rate, 1)} / {pct(tc?.hit_or_sack_rate, 1)}</span>
            <span className="k">PFR pressure rate</span><span>{pct(tc?.pfr_pressure_rate, 1, "PFR not published for these weeks")}</span>
          </div>
        </div>
        <div className={styles.contextCol}>
          <h3 className={styles.contextTitle}>Matchup <Badge value={L.matchup} tone={tone(L.matchup)} /></h3>
          {od ? (
            <div className="kv">
              <span className="k">Opponent</span><span>{od.defteam} <span className="faint">({od.games} games)</span></span>
              <span className="k">Rush EPA allowed</span><span>{num(od.rush_epa_allowed, 3)} <span className="faint">#{od.ranks.rush_epa_allowed}</span></span>
              <span className="k">Pass EPA allowed</span><span>{num(od.pass_epa_allowed, 3)} <span className="faint">#{od.ranks.pass_epa_allowed}</span></span>
              <span className="k">Rush success allowed</span><span>{pct(od.rush_success_allowed)}</span>
              <span className="k">{pos === "TE" ? "TE" : "RB"} target share allowed</span>
              <span>{pct(pos === "TE" ? od.te_target_share_allowed : od.rb_target_share_allowed)}</span>
            </div>
          ) : <p className="faint">{L.matchup === "BYE" ? "On bye this week." : "No opponent context."}</p>}
          <p className={styles.noteTight}>Rank #1 = most generous defense of 32 (season to date).</p>
        </div>
      </div>
      {pos !== "RB" && e.experimental && (
        <details className={styles.advanced}>
          <summary>Advanced / experimental</summary>
          <div className="kv faint">
            <span className="k" title={FTN_READ1_NOTE}>{FTN_READ1_LABEL}</span>
            <span>{pct(e.experimental.ftn_read1_target_share, 0, "Not charted by FTN")}{" "}
              <Badge value={e.experimental.read_semantics_status} tone="warn" title={FTN_READ1_NOTE} /></span>
          </div>
          <p>{FTN_READ1_NOTE}</p>
        </details>
      )}
      <p className={styles.confidence}>
        Data confidence: <Badge value={L.data_confidence} tone={tone(L.data_confidence)} /> · {e.rules?.data_confidence}
      </p>
    </Section>
  );
}

/** A player with a usable position gets a pointer to the remaining-schedule view. */
export function ScheduleLink({ id }: { id: number }) {
  return (
    <p className={styles.note}>
      <Link href={`/schedule-intelligence?player=${id}`}>Schedule intelligence</Link>: remaining opponents
      and what each defense has observed allowing (descriptive; separate from the projection in Outlook).
    </p>
  );
}

const ROUTE_ROWS: [keyof RouteData["season"], string, "int" | "pct" | "num"][] = [
  ["routes", "Routes", "int"], ["route_participation", "Route %", "pct"], ["tprr", "TPRR", "num"],
  ["yprr", "YPRR", "num"], ["pass_block_snaps", "Pass blocks", "int"], ["pass_block_rate", "Pass-block rate", "pct"],
  ["slot_rate", "Slot %", "pct"], ["wide_rate", "Wide %", "pct"], ["inline_rate", "Inline %", "pct"],
  ["backfield_rate", "Backfield %", "pct"], ["two_minute_route_share", "Two-minute role", "pct"],
  ["long_down_route_share", "Long-down role", "pct"], ["short_down_route_share", "Short-down role", "pct"],
  ["man_tprr", "TPRR vs man", "num"], ["zone_tprr", "TPRR vs zone", "num"],
];

export function RouteSection({ r }: { r: RouteData }) {
  const rows = ROUTE_ROWS.filter(([k]) => r.season[k] !== undefined && r.season[k] !== null);
  return (
    <Section
      title="Route participation"
      actions={<span className="faint">source {r.providers.join(", ")} ({r.datasets.join(", ")}) · updated {when(r.source_updated_at)} · weeks {r.weeks.join(", ")}</span>}
      footnote={<>
        {r.note} Season totals for counts, week averages for rates. External route metrics are shown for context only
        and do not affect scores or recommendations yet.
      </>}
    >
      <dl className={styles.routeGrid}>
        {rows.map(([k, text, kind]) => {
          const v = r.season[k] as number;
          const providerDefined = r.provider_defined.includes(k);
          return (
            <div key={k} className={styles.routeRow}>
              <dt>
                {text}
                {providerDefined && <> <Badge value="provider-defined" tone="warn" title="Definition belongs to the source provider; not normalized to an app-wide definition" /></>}
              </dt>
              <dd>{kind === "pct" ? pct(v) : kind === "int" ? int(v) : num(v, 2)}</dd>
            </div>
          );
        })}
      </dl>
    </Section>
  );
}
