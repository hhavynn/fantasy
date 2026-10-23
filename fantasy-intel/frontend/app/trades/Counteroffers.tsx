import { BackendProblem } from "@/components/BackendProblem";
import { TeamLink } from "@/components/TeamLink";
import { Delta, EmptyState, Notice, Section, SkeletonTable } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { cx } from "@/lib/cx";
import {
  counterofferAnalyzerHref, counterofferHref, coverageText, editText, statusTitle, toggleLockHref,
  type CounterofferResponse, type CounterofferSide,
} from "@/lib/counteroffers";
import { signed } from "@/lib/format";
import type { Selection } from "@/lib/trade-builder";
import styles from "./counteroffers.module.css";

// The analyzer's Counteroffer action (#214): up to three small changes to the evaluated offer that help the side it
// treats worse, each exactly evaluated for both teams by the backend and re-checked by the manual evaluator. Each opens
// in the builder; nothing is submitted to the provider and nothing estimates acceptance.

const TITLE = "Counteroffers";
const SUBTITLE = "Small changes to this offer that help the side it treats worse, both teams evaluated exactly";

/** Shown under the evaluation until the panel is opened: the search runs only when asked for. */
export function CounterofferAction({ selection }: { selection: Selection }) {
  return (
    <p className={styles.action}>
      <TeamLink href={counterofferHref(selection, [])} scroll={false}>Find counteroffers</TeamLink>
      <span className={styles.muted}> · up to three small changes to this offer</span>
    </p>
  );
}

function SideFigure({ name, side, tone }: { name: string; side: CounterofferSide; tone: "a" | "b" }) {
  return (
    <div className={cx(styles.side, tone === "a" ? styles.sideA : styles.sideB)}>
      <dt title={name}>{name}</dt>
      <dd>
        <Delta value={side.change_vs_original} /> <span className={styles.muted}>vs this offer</span>
      </dd>
      <dd className={styles.muted}>Rest of season {side.ros_delta === null ? "unknown" : signed(side.ros_delta, 1)}</dd>
    </div>
  );
}

export async function Counteroffers({ result, selection, locks }: {
  result: Promise<ApiResult<CounterofferResponse>>; selection: Selection; locks: number[];
}) {
  const r = await result;
  if (!r.ok) {
    return (
      <Section title={TITLE} subtitle={SUBTITLE}>
        {r.status === 409
          ? <Notice tone="caution" title="The league data changed">{r.error}</Notice>
          : <BackendProblem error={r.error} kind={r.kind} status={r.status} />}
      </Section>
    );
  }
  const d = r.data;
  const teamName = (id: number) => d.team_names[String(id)] || `Team ${id}`;
  const playerName = (id: number) => d.names[String(id)]?.name || `Player ${id}`;
  const offer = [...selection.a, ...selection.b];
  return (
    <Section title={TITLE} subtitle={SUBTITLE}
      footnote={<>{coverageText(d.search)} {d.note}</>}>
      <ul className={styles.locks} aria-label="Players kept in every counteroffer">
        <li className={styles.muted}>Keep in the deal:</li>
        {offer.map((id) => {
          const kept = locks.includes(id);
          return (
            <li key={id}>
              <TeamLink href={toggleLockHref(selection, locks, id)} scroll={false} className={cx(styles.lock, kept && styles.kept)}
                title={kept ? `${playerName(id)} stays in every counteroffer; select to release` : `Keep ${playerName(id)} in every counteroffer`}>
                {kept ? `Kept: ${playerName(id)}` : playerName(id)}
              </TeamLink>
            </li>
          );
        })}
      </ul>
      {d.status === "found" ? (
        <>
          <p className={styles.muted}>{statusTitle(d.status, d.results.length)}.</p>
          <ol className={styles.list}>
            {d.results.map((c) => (
              <li key={c.key} className={styles.item}>
                <ul className={styles.edits}>
                  {c.edits.map((e, i) => <li key={i}>{editText(d, e)}</li>)}
                </ul>
                <dl className={styles.sides}>
                  <SideFigure name={teamName(d.team_a_id)} side={c.team_a} tone="a" />
                  <SideFigure name={teamName(d.team_b_id)} side={c.team_b} tone="b" />
                </dl>
                {(c.team_a.forced_drops.length > 0 || c.team_b.forced_drops.length > 0) && (
                  <p className={styles.muted}>
                    Forced drops: {[...c.team_a.forced_drops, ...c.team_b.forced_drops].map(playerName).join(", ")}
                  </p>
                )}
                <p className={styles.action}>
                  <TeamLink href={counterofferAnalyzerHref(c)}>Open in the builder</TeamLink>
                </p>
              </li>
            ))}
          </ol>
        </>
      ) : d.status === "original_unknown" ? (
        <Notice tone="caution" title={statusTitle(d.status, 0)}>{d.reason}</Notice>
      ) : (
        <EmptyState title={statusTitle(d.status, 0)}>{d.reason}</EmptyState>
      )}
    </Section>
  );
}

/** Placeholder while the bounded search runs. */
export function CounteroffersSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Finding counteroffers">
      <Section title={TITLE} subtitle={SUBTITLE}>
        <SkeletonTable rows={3} cols={3} />
      </Section>
    </div>
  );
}
