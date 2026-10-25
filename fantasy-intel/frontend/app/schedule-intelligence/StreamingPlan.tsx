import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { Badge, Notice, Pos, ProvenanceBadge, Section, VisuallyHidden } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { cx } from "@/lib/cx";
import { withLeague } from "@/lib/league-scope";
import { Missing } from "@/lib/format";
import { projectionsPhrase } from "@/lib/labels";
import {
  ELIGIBILITY_TONE, ELIGIBILITY_WORD, POSITION_NAME, currentAdvice, opponentText, planSummary, positionPageLink,
  weekAction, weekAnchor, weekPoints, type KdstStreaming, type StreamPositionPlan, type StreamStart, type StreamWeek,
} from "@/lib/kdst-streaming";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import styles from "./kdst-streaming.module.css";

// Multi-week K and D/ST streaming planner (issue #181): one compact timeline per position. Each week is a short row
// (what to do, the projection, the eligibility), with a native disclosure for the reasons and alternatives and a link
// back to the kicker or defense page for that week. Every pick and number is the backend's; a week it did not plan
// shows no player and no number.

type Carry = { team: string | undefined; position: string; phase: string };

function Eligible({ s, rules }: { s: StreamStart; rules: KdstStreaming["rules"] }) {
  return <Badge value={ELIGIBILITY_WORD[s.eligibility]} tone={ELIGIBILITY_TONE[s.eligibility]} title={rules.eligibility[s.eligibility]} />;
}

function Points({ v, why }: { v: number | null | undefined; why: string }) {
  return v === null || v === undefined ? <Missing why={why} /> : <>{v.toFixed(1)}</>;
}

function Controls({ d, carry, league }: { d: KdstStreaming; carry: Carry; league: string | null }) {
  const q = d.query;
  return (
    <form action={`${withLeague("/schedule-intelligence", league)}#kdst-streaming`} className={styles.controls} aria-label="Streaming plan settings">
      {carry.team && <input type="hidden" name="team" value={carry.team} />}
      <input type="hidden" name="position" value={carry.position} />
      <input type="hidden" name="phase" value={carry.phase} />
      <label>Weeks <select name="stream_weeks" defaultValue={String(q.weeks)}>
        {[2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
      </select></label>
      <label>Max add/drops <select name="max_moves" defaultValue={q.max_moves === null ? "" : String(q.max_moves)}>
        <option value="">No limit</option>
        {[0, 1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
      </select></label>
      <label>Points per add/drop <input type="number" name="move_cost" min={0} max={d.rules.max_move_cost} step={0.5} defaultValue={q.move_cost} inputMode="decimal" /></label>
      <button type="submit">Plan</button>
    </form>
  );
}

function StartLine({ s, rules }: { s: StreamStart; rules: KdstStreaming["rules"] }) {
  const opp = opponentText(s);
  return (
    <>
      <b>{s.name ?? `Player #${s.espn_player_id}`}</b> <span className="faint">{s.pro_team}{opp ? ` ${opp}` : ""}</span>{" "}
      <Points v={s.points} why={s.detail ?? "No projection"} />{" "}
      <Eligible s={s} rules={rules} />
      {s.delta !== undefined && s.delta !== null && <span className="faint"> ({s.delta >= 0 ? "+" : "−"}{Math.abs(s.delta).toFixed(1)} vs plan)</span>}
      {s.model?.expected_points !== null && s.model?.expected_points !== undefined && (
        <span className="faint" title={`${s.model.model_version}, made ${s.model.forecast_horizon} ${plural(s.model.forecast_horizon, "week")} ahead`}> · model {s.model.expected_points.toFixed(1)}</span>
      )}
      {s.conflicts.length > 0 && <span className={styles.conflict}> · plays against your {s.conflicts.map((c) => c.name ?? `#${c.espn_player_id}`).join(", ")}</span>}
    </>
  );
}

function WeekItem({ w, p, d, league }: { w: StreamWeek; p: StreamPositionPlan; d: KdstStreaming; league: string | null }) {
  const anchor = weekAnchor(p.position, w.week);
  const v = weekPoints(w);
  const link = positionPageLink(p.position, w.week, league);
  const opp = w.start ? opponentText(w.start) : w.action === "hold" ? opponentText(w.hold) : null;
  return (
    <li id={anchor} data-testid={anchor} className={cx(styles.week, !w.planned && styles.capped, w.gap?.code === "empty_slot" && styles.gapWeek)}>
      <p className={styles.weekHead}>
        <b>Wk {w.week}</b>
        {w.phase === "playoffs" && <small> playoffs</small>}
        {w.outlook && w.planned && <Badge value="Outlook" tone="info" title="Two or more weeks ahead: K and D/ST projections this far out are least reliable" />}
        <span className={styles.pts}>{w.planned ? <Points v={v} why="No projection for the planned start" /> : <Missing why={w.reason ?? "Not planned"} />}</span>
      </p>
      <p className={styles.action}>
        {w.action === "add" && <span className={styles.move} aria-hidden="true">+ </span>}
        {weekAction(w, p.position)}
        {opp && <span className="faint"> {opp}</span>}
        {w.start && w.start.eligibility !== "held" && <> <Eligible s={w.start} rules={d.rules} /></>}
      </p>
      {!w.planned && <p className={styles.why}>{w.reason}</p>}
      {w.gap && <p className={styles.gap}>{w.gap.text}</p>}
      <div className={styles.foot}>
      {w.planned && (w.reasons.length > 0 || w.alternatives.length > 0) && (
        <details className={styles.more}>
          <summary>Why · {w.alternatives.length} {plural(w.alternatives.length, "alternative")}<VisuallyHidden> for {POSITION_NAME[p.position]} week {w.week}</VisuallyHidden></summary>
          <ul className={styles.reasons}>{w.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          {w.alternatives.length > 0 && (
            <ol className={styles.alts} aria-label={`Alternatives for week ${w.week}`}>
              {w.alternatives.map((a) => <li key={a.espn_player_id}><StartLine s={a} rules={d.rules} /></li>)}
            </ol>
          )}
        </details>
      )}
      <Link className={styles.pageLink} href={link}>
        {p.position === "K" ? "Kicker" : "Defense"} projections<VisuallyHidden>, week {w.week}</VisuallyHidden>
      </Link>
      </div>
    </li>
  );
}

function PositionPlan({ p, d, league }: { p: StreamPositionPlan; d: KdstStreaming; league: string | null }) {
  const first = p.weeks[0];
  const tw = p.this_week;
  return (
    <div className={styles.position} data-testid={`stream-${p.position === "K" ? "k" : "dst"}`}>
      <h3 className={styles.posTitle}><Pos value={p.position} /> {POSITION_NAME[p.position]}</h3>
      <p className={styles.advice} data-testid={`stream-advice-${p.position === "K" ? "k" : "dst"}`}>
        <b>{currentAdvice(p)}</b>
        {first && p.status !== "not_started" && <> <Link href={positionPageLink(p.position, first.week, league)}>Open {p.position === "K" ? "kicker" : "defense"} projections for week {first.week}</Link></>}
      </p>
      {tw?.best && (
        <p className={styles.best}>
          Best streamer, week {tw.week}: <StartLine s={tw.best} rules={d.rules} />
          {tw.best_confirmed && <> · best listed free agent: <StartLine s={tw.best_confirmed} rules={d.rules} /></>}
        </p>
      )}
      {p.status !== "not_started" && <p className={styles.summary}>{planSummary(p)}{p.pool && <span className="faint"> · {p.pool.candidates} of {p.pool.free_agents} free {plural(p.pool.free_agents, "agent")} considered{p.pool.excluded ? `, ${p.pool.excluded} excluded` : ""}{p.pool.unprojected ? `, ${p.pool.unprojected} without a projection` : ""}</span>}</p>}
      {p.plan === null && p.no_plan_reason && p.status === "no_plan" && <Notice tone="caution">{p.no_plan_reason}</Notice>}
      {p.weeks.length > 0 && (
        <ol className={styles.timeline} aria-label={`${POSITION_NAME[p.position]} streaming plan by week`}>
          {p.weeks.map((w) => <WeekItem key={w.week} w={w} p={p} d={d} league={league} />)}
        </ol>
      )}
      {p.excluded.length > 0 && (
        <p className={styles.excluded}>Not considered: {p.excluded.map((e) => `${e.name ?? `#${e.espn_player_id}`} (${e.reason})`).join("; ")}.</p>
      )}
    </div>
  );
}

export function StreamingPlanSection({ res, league, carry }: { res: ApiResult<KdstStreaming>; league: string | null; carry: Carry }) {
  if (!res.ok) {
    return (
      <Section id="kdst-streaming" title="K and D/ST streaming plan">
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Section>
    );
  }
  const d = res.data;
  const w = d.window;
  return (
    <Section
      id="kdst-streaming"
      title="K and D/ST streaming plan"
      subtitle={<>{d.team.name} · free agents only · {projectionsPhrase(d.projection_source)} · weeks {w.weeks[0] ?? "—"}–{w.weeks[w.weeks.length - 1] ?? "—"}. The position and weeks filters above do not apply.</>}
      actions={<ProvenanceBadge signals={signalsOf(d.provenance)} />}
      footnote={<>One streaming slot per position: each week either keeps your current player or adds one free agent, never one on bye and never one without a projection. Free agent: listed at the last sync. Waiver claim: needs a claim. If still available: a later week&apos;s add, not confirmed. The plan stops at the first week with fewer than {d.rules.min_projected} projected free agents.</>}
    >
      <Controls d={d} carry={carry} league={league} />
      {!w.remaining_weeks && <Notice tone="caution">No remaining fantasy weeks to plan: every week through the final scoring period has kicked off.</Notice>}
      {w.uncertain && w.warning && <Notice tone="caution">{w.warning}</Notice>}
      {!d.schedule.available && <Notice tone="caution">No NFL schedule is loaded, so no bye or opponent is known and no week is planned.</Notice>}
      <div className={styles.positions}>
        {d.positions.map((p) => <PositionPlan key={p.position} p={p} d={d} league={league} />)}
      </div>
      <details className={styles.notes}>
        <summary>How the plan is made</summary>
        <ul className="reasons">{d.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      </details>
    </Section>
  );
}
