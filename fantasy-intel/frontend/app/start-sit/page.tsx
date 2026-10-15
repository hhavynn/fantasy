import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Callout, ErrorBox, Pos } from "@/components/ui";
import { api } from "@/lib/api";
import { label, num, when } from "@/lib/format";
import {
  actionabilityBadgeTone,
  formatGain,
  lockStatusBadge,
} from "@/lib/start-sit-helpers";
import { StartSitControls } from "./StartSitControls";
import type { LineupPlayer, StartSitResponse } from "./types";
import styles from "./start-sit.module.css";

export const metadata = { title: "Start/Sit" };

interface PageProps {
  searchParams: Promise<{
    team_id?: string;
    week?: string;
  }>;
}

export default async function StartSitPage({ searchParams }: PageProps) {
  const sp = await searchParams;
  const qs = new URLSearchParams();
  if (sp.team_id) qs.set("team_id", sp.team_id);
  if (sp.week) qs.set("week", sp.week);

  const res = await api<StartSitResponse>(`/api/start-sit?${qs.toString()}`);
  if (!res.ok) {
    return (
      <div className={`stack ${styles.page}`}>
        <div className="page-head">
          <h1>Weekly Start/Sit Command Center</h1>
        </div>
        <ErrorBox error={res.error} />
      </div>
    );
  }

  const data = res.data;
  const {
    league,
    team,
    teams = [],
    scoring_period,
    is_future_preview,
    summary,
    recommended_lineup,
    decisions,
    current_bench,
    injury_warnings,
    schedule_warnings,
    freshness,
    limitations,
  } = data;

  const statusLabelMap: Record<string, { label: string; tone: "good" | "info" | "warn" | "bad" }> = {
    optimal: { label: "Lineup Optimal", tone: "good" },
    upgrade_available: { label: "Upgrades Available", tone: "info" },
    incomplete_data: { label: "Incomplete Projections", tone: "warn" },
    all_locked: { label: "All Games Locked", tone: "info" },
    future_preview: { label: "Future Preview", tone: "info" },
  };

  const statusInfo = statusLabelMap[summary.status] || {
    label: label(summary.status),
    tone: "info" as const,
  };

  return (
    <div className={`stack ${styles.page}`}>
      {/* Page Header */}
      <div className="page-head">
        <div>
          <h1>Weekly Start/Sit Command Center</h1>
          <span className="sub">
            {team.name} ({team.abbrev}) · Week {scoring_period} · Source: {data.projection_source} Projections
          </span>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Badge value={statusInfo.label} tone={statusInfo.tone} />
        </div>
      </div>

      {/* Interactive Controls Bar: Team Dropdown & Week Toggle */}
      <StartSitControls
        currentTeamId={team.id}
        teams={teams.length > 0 ? teams : [team]}
        currentWeek={league.current_week}
        selectedWeek={scoring_period}
        finalWeek={league.final_scoring_period}
      />

      {is_future_preview && (
        <Callout>
          <b>Future Week Preview (Week {scoring_period}):</b> Lineup assignments represent an informational projection based on current roster assets. ESPN does not accept active lineup submissions for future scoring periods until the current week concludes.
        </Callout>
      )}

      {/* Section A: Weekly Lineup Summary */}
      <section className={styles.metricsGrid} aria-label="Lineup Summary Metrics">
        <div className={styles.metricCard}>
          <div className={styles.metricHeader}>
            <span className={styles.metricLabel}>Actual Lineup Total</span>
            <Badge value="ESPN Starters" />
          </div>
          <div className={styles.metricValue}>
            {summary.starters_count === 0
              ? "—"
              : summary.projection_complete && summary.actual_projected_total !== null
                ? `${num(summary.actual_projected_total, 1)} pts`
                : `${num(summary.known_actual_total, 1)} pts*`}
          </div>
          <span className={styles.metricSubtext}>
            {summary.starters_count === 0
              ? "No submitted lineup for this week"
              : summary.projection_complete ? "All starters projected" : "*Partial total (missing projections)"}
          </span>
        </div>

        <div className={styles.metricCard}>
          <div className={styles.metricHeader}>
            <span className={styles.metricLabel}>Recommended Total</span>
            <Badge value="Lock-Constrained" tone="info" />
          </div>
          <div className={styles.metricValue}>
            {summary.projection_complete && summary.recommended_projected_total !== null
              ? `${num(summary.recommended_projected_total, 1)} pts`
              : `${num(summary.known_recommended_total, 1)} pts*`}
          </div>
          <span className={styles.metricSubtext}>
            Respects confirmed kickoff locks
          </span>
        </div>

        <div className={styles.metricCard}>
          <div className={styles.metricHeader}>
            <span className={styles.metricLabel}>Projected Net Gain</span>
            {summary.projection_complete && summary.projected_gain !== null && summary.projected_gain > 0 ? (
              <Badge value="Upgrade" tone="good" />
            ) : null}
          </div>
          <div
            className={`${styles.metricValue} ${
              summary.projected_gain && summary.projected_gain > 0
                ? styles.gainPositive
                : styles.gainZero
            }`}
          >
            {formatGain(summary.projected_gain)}
          </div>
          <span className={styles.metricSubtext}>
            {summary.projected_gain && summary.projected_gain > 0
              ? "Gain over actual ESPN lineup"
              : summary.status === "incomplete_data"
              ? "Cannot compute (missing projections)"
              : summary.projected_gain === null
              ? "No submitted lineup to compare"
              : "Currently starting best lineup"}
          </span>
        </div>

        <div className={styles.metricCard}>
          <div className={styles.metricHeader}>
            <span className={styles.metricLabel}>Roster Status</span>
          </div>
          <div className={styles.metricValue} style={{ fontSize: 20 }}>
            {summary.starters_count} Starters · {summary.bench_count} Bench
          </div>
          <span className={styles.metricSubtext}>
            {summary.locked_players_count} of {summary.starters_count + summary.bench_count + summary.ir_count} players kicked off
          </span>
        </div>
      </section>

      {/* Unconstrained Optimum Benchmark Bar */}
      <div className={styles.benchmarkBar}>
        <span>
          <b>Theoretical Benchmark (Unconstrained Optimum):</b>{" "}
          {summary.unconstrained_total !== null
            ? `${num(summary.unconstrained_total, 1)} pts`
            : `${num(summary.known_unconstrained_total, 1)} pts*`}
          {" "}(Maximum achievable points assuming zero kickoff lock restrictions)
        </span>
        {summary.unconstrained_total !== null && summary.recommended_projected_total !== null && (
          <span>
            Lock Drag: -{num(Math.max(0, summary.unconstrained_total - summary.recommended_projected_total), 1)} pts
          </span>
        )}
      </div>

      {/* Section B: Start/Sit Recommendations */}
      <section className={styles.section} aria-label="Start/Sit Recommendations">
        <div className={styles.sectionHeading}>
          <h2>Start/Sit Recommendations</h2>
          <span className={styles.sectionSub}>
            {decisions.length === 0 || decisions[0]?.action === "NO_CHANGE"
              ? "0 moves needed"
              : `${decisions.length} recommended ${decisions.length === 1 ? "move" : "moves"}`}
          </span>
        </div>

        {decisions.length === 0 || decisions[0]?.action === "NO_CHANGE" ? (
          <div className={styles.emptyState}>
            <h3>Your Lineup is Already Optimal</h3>
            <p>
              Your currently submitted ESPN starting lineup maximizes projected points across all eligible, unlocked roster options. No swaps or bench promotions are recommended for Week {scoring_period}.
            </p>
          </div>
        ) : (
          <div className={styles.decisionsList}>
            {decisions.map((decision) => {
              const tone = actionabilityBadgeTone(decision.actionability);
              const isActionable = decision.actionability === "ACTIONABLE";

              return (
                <article
                  key={decision.id}
                  className={`${styles.decisionCard} ${isActionable ? styles.actionable : ""}`}
                >
                  <div className={styles.decisionTop}>
                    <div className={styles.decisionActionHeader}>
                      <span
                        className={`${styles.actionBadge} ${
                          decision.action === "COORDINATED_SWAP" ? styles.cascade : styles.swap
                        }`}
                      >
                        {decision.action === "COORDINATED_SWAP" ? "FLEX Cascade" : "1-for-1 Swap"}
                      </span>
                      <Badge
                        value={label(decision.actionability)}
                        tone={tone === "accent" ? "good" : tone === "warn" ? "warn" : "info"}
                      />
                      {decision.target_slot && (
                        <span className={styles.slotTarget}>Slot: {decision.target_slot}</span>
                      )}
                    </div>
                    {decision.projected_net_gain !== null && (
                      <span className={styles.gainBadge}>
                        +{num(decision.projected_net_gain, 1)} pts projected gain
                      </span>
                    )}
                  </div>

                  <div className={styles.decisionBody}>
                    {/* Players to START */}
                    <div className={`${styles.playerColumn} ${styles.start}`}>
                      <span className={styles.columnRole}>Start (From Bench)</span>
                      {decision.start_players.map((p) => (
                        <div key={p.id}>
                          <div className={styles.playerName}>
                            <PlayerLink id={p.id} name={p.name} /> <Pos value={p.position} />
                          </div>
                          <div className={styles.playerMeta}>
                            <span>{p.pro_team}</span>
                            <span className={styles.playerProjection}>{num(p.projection, 1)} pts</span>
                          </div>
                        </div>
                      ))}
                    </div>

                    <div className={styles.swapArrow} aria-hidden="true">
                      <span>⇄</span>
                    </div>

                    {/* Players to BENCH */}
                    <div className={`${styles.playerColumn} ${styles.bench}`}>
                      <span className={styles.columnRole}>Bench (Current Starter)</span>
                      {decision.bench_players.map((p) => (
                        <div key={p.id}>
                          <div className={styles.playerName}>
                            <PlayerLink id={p.id} name={p.name} /> <Pos value={p.position} />
                          </div>
                          <div className={styles.playerMeta}>
                            <span>{p.pro_team}</span>
                            <span className={styles.playerProjection}>{num(p.projection, 1)} pts</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <p className={styles.explanation}>{decision.explanation}</p>

                  {decision.warnings && decision.warnings.length > 0 && (
                    <div className={styles.decisionWarnings}>
                      <b>Constraints &amp; Notice:</b>
                      <ul>
                        {decision.warnings.map((w, idx) => (
                          <li key={idx}>{w}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* Section C: Complete Recommended Lineup */}
      <section className={styles.section} aria-label="Complete Recommended Lineup">
        <div className={styles.sectionHeading}>
          <h2>Recommended Starting Lineup</h2>
          <span className={styles.sectionSub}>Slot-by-slot lineup under lock constraints</span>
        </div>

        <div className="panel tight">
          <div className="table-wrap">
            <table className={styles.lineupTable}>
              <thead>
                <tr>
                  <th>Slot</th>
                  <th>Player</th>
                  <th>Opponent / Kickoff</th>
                  <th>Current Status</th>
                  <th className="num">Projected</th>
                </tr>
              </thead>
              <tbody>
                {recommended_lineup.map((row: LineupPlayer) => {
                  const lockInfo = lockStatusBadge(
                    row.lock_status,
                    row.kickoff_utc,
                    row.has_final_score,
                    row.home_score,
                    row.away_score
                  );

                  return (
                    <tr key={row.slot_id}>
                      <td className={styles.slotCell}>
                        <b>{row.slot}</b>
                      </td>
                      <td>
                        {row.player_id ? (
                          <div className={styles.playerCell}>
                            <div className={styles.playerMain}>
                              <PlayerLink id={row.player_id} name={row.name} />
                              <Pos value={row.position} />
                              {row.injury_status && !["ACTIVE", "NORMAL"].includes(row.injury_status) && (
                                <Badge value={row.injury_status} />
                              )}
                            </div>
                            <div className={styles.playerSub}>
                              <span>{row.pro_team ?? "FA"}</span>
                              {row.is_flex && <span className="faint">(in FLEX slot)</span>}
                            </div>
                          </div>
                        ) : (
                          <span className="missing">Empty Slot</span>
                        )}
                      </td>
                      <td>
                        <div className={styles.playerSub}>
                          {row.opponent ? (
                            <span>
                              {row.is_home ? "vs" : "@"} {row.opponent}
                            </span>
                          ) : (
                            <span className="faint">{row.lock_status === "BYE" ? "BYE" : "No game"}</span>
                          )}
                          <Badge
                            value={lockInfo.label}
                            tone={lockInfo.tone === "info" ? "info" : lockInfo.tone === "warn" ? "warn" : ""}
                          />
                        </div>
                      </td>
                      <td>
                        {row.is_currently_starting ? (
                          <span className={`${styles.statusIndicator} ${styles.starting}`}>
                            ✓ Started in ESPN
                          </span>
                        ) : (
                          <span className={`${styles.statusIndicator} ${styles.promoted}`}>
                            ★ Promoted from Bench
                          </span>
                        )}
                      </td>
                      <td className="num">
                        <b>{num(row.projected_points, 1)}</b>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* Section D: Current Bench & Depth */}
      <section className={styles.section} aria-label="Current Bench Players">
        <div className={styles.sectionHeading}>
          <h2>Current Bench &amp; Reserves</h2>
          <span className={styles.sectionSub}>{current_bench.length} players</span>
        </div>

        <div className="panel tight">
          <div className="table-wrap">
            <table className={styles.lineupTable}>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Opponent / Kickoff</th>
                  <th>Designation</th>
                  <th>Lock Status</th>
                  <th className="num">Projected</th>
                </tr>
              </thead>
              <tbody>
                {current_bench.map((b) => {
                  const lockInfo = lockStatusBadge(b.lock_status, b.kickoff_utc);
                  const isPromoted = recommended_lineup.some((r) => r.player_id === b.player_id);

                  return (
                    <tr key={b.player_id}>
                      <td>
                        <div className={styles.playerCell}>
                          <div className={styles.playerMain}>
                            <PlayerLink id={b.player_id} name={b.name} />
                            <Pos value={b.position} />
                            {b.on_ir && <Badge value="IR Slot" tone="bad" />}
                            {b.nfl_ineligible && <Badge value="NFL Out" tone="bad" />}
                            {b.injury_status && !["ACTIVE", "NORMAL"].includes(b.injury_status) && (
                              <Badge value={b.injury_status} />
                            )}
                          </div>
                          <div className={styles.playerSub}>
                            <span>{b.pro_team ?? "FA"}</span>
                          </div>
                        </div>
                      </td>
                      <td>
                        <div className={styles.playerSub}>
                          {b.opponent ? (
                            <span>
                              {b.is_home ? "vs" : "@"} {b.opponent}
                            </span>
                          ) : (
                            <span className="faint">{b.lock_status === "BYE" ? "BYE" : "No game"}</span>
                          )}
                        </div>
                      </td>
                      <td>
                        {isPromoted ? (
                          <span className={`${styles.statusIndicator} ${styles.promoted}`}>
                            Recommended Starter
                          </span>
                        ) : (
                          <span className={`${styles.statusIndicator} ${styles.starting}`}>
                            Bench Reserve
                          </span>
                        )}
                      </td>
                      <td>
                        <Badge
                          value={lockInfo.label}
                          tone={lockInfo.tone === "info" ? "info" : lockInfo.tone === "warn" ? "warn" : ""}
                        />
                      </td>
                      <td className="num">{num(b.projected_points, 1)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* Section E: Injury, Uncertainty & Warnings */}
      {(injury_warnings.length > 0 || schedule_warnings.length > 0) && (
        <section className={styles.section} aria-label="Lineup Warnings">
          <div className={styles.warningBox}>
            <div className={styles.warningTitle}>
              <span>⚠</span> Injury &amp; Availability Alerts
            </div>
            {injury_warnings.map((w) => (
              <div key={w.player_id} className={styles.warningItem}>
                <b>{w.name}</b> ({w.position}): {w.warnings.join("; ")}
              </div>
            ))}
            {schedule_warnings.map((sw, idx) => (
              <div key={idx} className={styles.warningItem}>
                <b>Schedule Alert:</b> {sw}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Freshness & Limitations Footer */}
      <footer className={styles.freshnessFooter}>
        <div className={styles.freshnessRow}>
          <span>
            Lineup Snapshot: <b>{when(freshness.lineup_synced_at)}</b>
          </span>
          <span>
            ESPN Projections: <b>{when(freshness.projection_synced_at)}</b>
          </span>
          <span>
            NFL Schedule: <b>{when(freshness.nfl_schedule_synced_at)}</b>
          </span>
        </div>
        <ul className={styles.limitationsList}>
          {limitations.map((limit, idx) => (
            <li key={idx}>{limit}</li>
          ))}
        </ul>
      </footer>
    </div>
  );
}
