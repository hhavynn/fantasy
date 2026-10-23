import Link from "@/components/LeagueLink";
import { Notice, Section } from "@/components/ui";
import { num, signed } from "@/lib/format";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import {
  MEASURES, MEASURE_LABEL, PATH, PRESETS, SCRIPTS, SCRIPT_LABEL, SCRIPT_RULE, coverageText, mixText, paceHref,
  presetActive, type Backtest, type PaceLabResponse, type PaceState, type Range,
} from "@/lib/pace-lab";
import styles from "./pace-lab.module.css";

function RangeCell({ r, why }: { r: Range; why: string }) {
  return (
    <td className="num">
      {num(r.expected, 1, why)}
      <span className={styles.sub}>
        {r.low !== null && r.high !== null ? `${r.low.toFixed(1)} to ${r.high.toFixed(1)}` : <span title={why}>no range</span>}
      </span>
    </td>
  );
}

/**
 * The hypothetical script mix: a GET form (three percent weights) with one-tap presets, and the resulting play volume
 * and opportunities against the team's observed mix. Every number is the backend's; the page only lays them out.
 */
export async function Scenario({ state, d, bt, sentence }: {
  state: PaceState; d: PaceLabResponse; bt: Backtest; sentence: string;
}) {
  const sc = d.scenario!;
  const team = d.team!.team;
  const action = withLeague(PATH, (await currentLeague())?.id);
  const pct = sc.mix ? sc.mix.map((x) => Math.round(x * 100)) : null;
  const why = sc.result.range_reason ?? "Not enough past games to measure";
  const cond = bt.status === "frozen" ? coverageText(bt.range_coverage.conditional_on_script) : null;
  return (
    <Section title="Hypothetical script mix" subtitle={`What ${team}'s play volume and opportunities looked like in past games shaped like the mix you choose`}>
      <Notice tone="caution" title="Hypothetical, not a forecast">
        This scenario does not say how any future game will go. It combines {team}&apos;s per-script estimates (model
        assumptions, below) with the spread of past games. {sentence}
      </Notice>
      <nav className="filters" aria-label="Preset mixes">
        <span className={styles.chipLabel}>Presets</span>
        {PRESETS.map((p) => (
          <Link key={p.key} className={`chip ${presetActive(state.mix, p) ? "active" : ""}`}
            aria-current={presetActive(state.mix, p) ? "true" : undefined}
            href={paceHref(state, { mix: p.mix ? [...p.mix] as [number, number, number] : null })} scroll={false} prefetch={false}>
            {p.label}
          </Link>
        ))}
      </nav>
      <form className={styles.mixForm} action={action} method="get" aria-label="Script mix">
        {state.season !== null && <input type="hidden" name="season" value={state.season} />}
        {state.seasons !== 1 && <input type="hidden" name="seasons" value={state.seasons} />}
        <input type="hidden" name="team" value={team} />
        {state.opp && <input type="hidden" name="opp" value={state.opp} />}
        {state.window !== "season" && <input type="hidden" name="window" value={state.window} />}
        {state.through !== null && <input type="hidden" name="through" value={state.through} />}
        {SCRIPTS.map((s, i) => (
          <label key={s} className={styles.field}>
            {SCRIPT_LABEL[s]} %
            <input type="number" name={["mc", "ml", "mt"][i]} min={0} max={100} step={5} inputMode="numeric"
              defaultValue={pct ? pct[i] : undefined} aria-describedby={`rule-${s}`} />
            <span id={`rule-${s}`} className={styles.sub}>{SCRIPT_RULE[s]}</span>
          </label>
        ))}
        <button type="submit">Apply mix</button>
      </form>
      <p className={styles.note}>
        Scenario: {mixText(sc.mix)}{sc.input_mix && sc.input_mix.reduce((a, b) => a + b, 0) !== 100 ? " (weights rescaled to 100%)" : ""}.
        {" "}Compared with {sc.baseline_source === "observed" ? `${team}'s observed mix in this filter`
          : sc.baseline_source === "league" ? "the league's mix (no classified game for this team)"
            : "no mix (no game in this filter has a final score to classify)"}:
        {" "}{mixText(sc.baseline_mix)}.
      </p>
      {!sc.available ? (
        <Notice tone="caution" title="Scenario unavailable">
          No centre or range can be given: {sc.unavailable_reason ?? "the per-script estimates are missing"}. Nothing here is a measured zero.
        </Notice>
      ) : (
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Per game</th>
              <th scope="col" className="num">Observed mix <span className={styles.sub}>centre · range</span></th>
              <th scope="col" className="num">Scenario <span className={styles.sub}>centre · range</span></th>
              <th scope="col" className="num">Change in centre</th>
            </tr>
          </thead>
          <tbody>
            {MEASURES.map((m) => (
              <tr key={m}>
                <th scope="row">{MEASURE_LABEL[m]}</th>
                <RangeCell r={sc.baseline.measures[m]} why={sc.baseline.range_reason ?? why} />
                <RangeCell r={sc.result.measures[m]} why={why} />
                <td className="num">{signed(sc.change[m].expected)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
      <p className={styles.note}>
        Range: the middle 80% of past team-games shaped like the mix (10th to 90th percentile), not a probability.
        {cond ? ` In the backtest, the range for the script that actually happened contained ${cond}.` : ""}
      </p>
    </Section>
  );
}
