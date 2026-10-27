import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerLink } from "@/components/PlayerLink";
import { ProvenanceBadge, Section, Notice } from "@/components/ui";
import { api } from "@/lib/api";
import { signalsOf, type ProvenancePayload } from "@/lib/signal-provenance";
import { stashGain, STASH_HORIZONS, type StashHorizon } from "@/lib/stash-horizon";
import { withParams } from "@/lib/waivers";
import { SavePlayer } from "./Shortlist";
import type { SearchParams } from "@/lib/sort";
import styles from "./stash-panel.module.css";

type StashRow = {
  espn_player_id: number; name: string | null; position: string; pro_team: string | null;
  availability: string; this_week_delta: number | null; later_delta: number | null;
  horizon_delta: number | null; first_start_week: number | null; why: string;
  weeks: { week: number; delta: number | null; starts: boolean }[];
};
type Contingency = {
  espn_player_id: number; name: string | null; team: string | null;
  basis: "observed" | "hypothesis" | string;
  if: { kind: string; starter: string | null };
  reasons: string[];
};
type Plan = {
  contract: string; status: string; horizon: number; now_week: number | null;
  team?: { name: string; team_id: number } | null;
  stashes: StashRow[]; pick_up_now: { espn_player_id: number }[];
  drop: { name: string | null; horizon_cost: number | null } | null;
  drop_basis: string | null; drop_notes: string[]; reason: string | null;
  projection_source: string; notes: string[];
  counts: { considered?: number; evaluated?: number; excluded_unprojected?: number; excluded_position_limit?: number;
    not_evaluated_cap?: number; unknown?: number; pick_up_now?: number; stashes?: number };
  contingencies?: { available: boolean; reason?: string; items: Contingency[]; note?: string } | null;
  provenance?: ProvenancePayload | null;
};

/** Read-only: all values and roster legality come from the existing authorized stash-plan/v1 endpoint. */
export async function StashPanel({ horizon, teamId, sp }: {
  horizon: StashHorizon; teamId: number | null; sp: SearchParams;
}) {
  const choices = (
    <nav className={styles.horizons} aria-label="Stash planning horizon">
      {STASH_HORIZONS.map((weeks) => (
        <Link key={weeks} className={`chip ${weeks === horizon ? "active" : ""}`}
          aria-current={weeks === horizon ? "page" : undefined}
          href={withParams(sp, { stashHorizon: String(weeks), offset: null })}>
          {weeks} weeks
        </Link>
      ))}
    </nav>
  );
  // The waiver response is the authority on the analyzed roster. Do not trigger a separate default-team 404.
  if (teamId === null) {
    return (
      <Section title="Future-week stashes" subtitle="Separate from this week&apos;s pickups">
        {choices}
        <p>Pick a team above to see a roster-specific stash plan.</p>
      </Section>
    );
  }
  const qs = new URLSearchParams({ horizon: String(horizon), team_id: String(teamId) });
  const res = await api<Plan>(`/api/my-team/stash?${qs}`);
  if (!res.ok) {
    return (
      <Section title="Future-week stashes" subtitle="Separate from this week&apos;s pickups">
        {choices}
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Section>
    );
  }
  const p = res.data;
  if (p.contract !== "stash-plan/v1") {
    return <Section title="Future-week stashes">{choices}<p>The stash analysis format is unavailable. Refresh the league snapshot.</p></Section>;
  }
  const noProjection = p.counts.excluded_unprojected ?? 0;
  const unevaluated = p.counts.not_evaluated_cap ?? 0;
  return (
    <Section
      title="Future-week stashes"
      subtitle={`For ${p.team?.name ?? "the selected roster"} · ${horizon} later weeks, plus the current week · ${p.projection_source} projections`}
      footnote="Projected changes in optimal starting-lineup points, not a guarantee of acquisition or future injury. Verify waiver status and roster rules before acting."
    >
      <div className={styles.body}>
        {choices}
        <div className={styles.meta}>
          <ProvenanceBadge signals={signalsOf(p.provenance)} />
          <span>Compared {p.counts.considered ?? "—"} eligible candidates · evaluated {p.counts.evaluated ?? "—"}</span>
          {(noProjection > 0 || unevaluated > 0 || (p.counts.unknown ?? 0) > 0) && (
            <span>{noProjection} excluded for missing projections · {unevaluated} beyond evaluation cap · {p.counts.unknown ?? 0} unknown</span>
          )}
        </div>
        {p.status !== "evaluated" ? (
          <Notice tone="caution" title="Stash value cannot be evaluated">{p.reason ?? "The roster or its projections are incomplete."}</Notice>
        ) : p.stashes.length ? (
          <ol className={styles.cards}>
            {p.stashes.map((s) => (
              <li key={s.espn_player_id} className={styles.card}>
                <div className={styles.player}>
                  <h3><PlayerLink id={s.espn_player_id} name={s.name} /></h3>
                  <span>{s.position}{s.pro_team ? ` · ${s.pro_team}` : ""} · {s.availability === "WAIVERS" ? "Waiver claim required" : "Listed free agent"}</span>
                </div>
                <div className={styles.gains}>
                  <strong>{stashGain(s.horizon_delta)} <span>over horizon</span></strong>
                  <span>This week {stashGain(s.this_week_delta)} · later {stashGain(s.later_delta)}</span>
                </div>
                <p className={styles.reason}>{s.why}</p>
                {s.first_start_week !== null && <p className={styles.detail}>First projected start: week {s.first_start_week}</p>}
                <SavePlayer id={s.espn_player_id} name={s.name ?? `Player #${s.espn_player_id}`} />
              </li>
            ))}
          </ol>
        ) : (
          <p className={styles.reason}>{p.reason ?? "No later-week stash adds projected lineup points over this window."}</p>
        )}
        {p.drop && (
          <p className={styles.detail}>Simulated drop: <strong>{p.drop.name ?? "Unnamed roster player"}</strong> · projected horizon cost {stashGain(p.drop.horizon_cost)}. This is a scenario, not a suggested transaction.</p>
        )}
        {p.drop_basis === "roster_count_unverified" && <Notice tone="caution" title="Roster capacity unverified">An open roster slot cannot be assumed. The analysis simulates a drop; confirm your provider&apos;s limits.</Notice>}
        {p.drop_notes?.length > 0 && <details><summary>Drop and roster limits</summary><ul>{p.drop_notes.map((n,i)=><li key={i}>{n}</li>)}</ul></details>}
        {(p.pick_up_now?.length ?? 0) > 0 && <p className={styles.detail}>{p.pick_up_now.length} possible current-week {p.pick_up_now.length === 1 ? "add" : "adds"} excluded from the stash list. Use the immediate claims above for this week&apos;s moves.</p>}
        {p.contingencies?.available && p.contingencies.items.length > 0 && (
          <div className={styles.contingency}>
            <h3>Conditional opportunities</h3>
            <p>Only if the named roster change happens. Observed samples and hypotheses are not injury probabilities or lineup-value forecasts.</p>
            <ul>
              {p.contingencies.items.slice(0, 5).map((s) => (
                <li key={`${s.espn_player_id}-${s.if.kind}-${s.if.starter}`}>
                  <PlayerLink id={s.espn_player_id} name={s.name} /> · {s.team ?? "NFL team unknown"} ·
                  {" "}{s.basis === "observed" ? "observed" : "hypothesis"} ·
                  {" "}{s.if.kind.replaceAll("_", " ")}: {s.if.starter ?? "starter unknown"}
                  {s.reasons[0] ? <span className={styles.detail}> · {s.reasons[0]}</span> : null}
                </li>
              ))}
            </ul>
            {p.contingencies.items.length > 5 && <p className={styles.detail}>Showing 5 of {p.contingencies.items.length} conditional scenarios.</p>}
          </div>
        )}
        <details>
          <summary>How the stash plan is computed</summary>
          <p>For each eligible free agent, the existing exact lineup evaluator compares this roster across the current week and {horizon} future weeks, with and without the add and one simulated drop. A stash must add net projected points over the entire window without helping this week. Missing projections are excluded rather than filled with zero. K and D/ST belong in weekly streaming, not this multi-week plan.</p>
        </details>
      </div>
    </Section>
  );
}
