import { BackendProblem } from "@/components/BackendProblem";
import { BarChart } from "@/components/charts";
import { EmptyState, Notice, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import {
  CONTRACT, SOURCE_NAME, chartMovers, eyebrowText, freshnessText, moversQuery, parseMoversState, selectedMover, signedPts,
  totalsText, type MoversResponse,
} from "@/lib/projection-movers";
import type { SearchParams } from "@/lib/sort";
import Link from "next/link";
import { Controls } from "./Controls";
import { Detail } from "./Detail";
import { Answer, RisersFallers } from "./Lists";
import { MoversMethod } from "./MoversMethod";
import { MoversTable } from "./MoversTable";
import { AboutComparison } from "./Notices";
import styles from "./movers.module.css";

export const metadata = { title: "Projection movers" };

const TITLE = "Projection movers";
const INTRO = "Whose projection changed since the last stored forecast, by how much, and what the data recorded alongside it.";

export default async function MoversPage({ searchParams }: PageProps<"/movers">) {
  const sp = (await searchParams) as SearchParams;
  let state = parseMoversState(sp);

  let res = await api<MoversResponse>(`/api/projections/movers?${moversQuery(state)}`);
  let baselineDropped = false;
  if (!res.ok && res.status === 422 && state.baseline) {
    // A stale link can name a comparison point that no longer exists: fall back to the default one, and say so.
    state = { ...state, baseline: null };
    baselineDropped = true;
    res = await api<MoversResponse>(`/api/projections/movers?${moversQuery(state)}`);
  }
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Players · Research" title={TITLE} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  if (d.contract !== CONTRACT) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Players · Research" title={TITLE} />
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response and the backend answered with {d.contract ? `“${d.contract}”` : "no version"}. Update the backend and the app together.
        </Notice>
      </div>
    );
  }

  const controls = (
    <Controls state={state} baselines={d.baselines} currentId={d.current?.id ?? null} effectiveBaseline={d.baseline?.id ?? null} />
  );
  const dropped = baselineDropped && (
    <Notice tone="info" title="That comparison point is no longer available">The default comparison is shown instead.</Notice>
  );

  if (!d.available) {
    return (
      <div className="stack">
        <PageHeader eyebrow={eyebrowText(null)} title={TITLE} meta={INTRO}>{controls}</PageHeader>
        {dropped}
        <Section title="Comparison">
          <EmptyState
            title={`${SOURCE_NAME[d.source]} cannot be compared right now`}
            action={d.source === "independent" ? <Link href="/movers" prefetch={false}>Show ESPN projections</Link> : undefined}
          >
            {d.reason ?? "There are not enough stored forecasts to show movement."}
          </EmptyState>
        </Section>
        <AboutComparison d={d} />
        <MoversMethod d={d} />
      </div>
    );
  }

  const { mover, requestedMissing } = selectedMover(d, state.player);
  const bars = chartMovers(d);
  const meta = (
    <>
      {freshnessText(d)}
    </>
  );

  return (
    <div className="stack">
      <PageHeader eyebrow={eyebrowText(d)} title={TITLE} meta={meta}>{controls}</PageHeader>
      {dropped}
      <Answer d={d} />
      <RisersFallers d={d} state={state} selectedId={mover?.espn_player_id ?? null} />
      <AboutComparison d={d} />

      {bars.length > 0 && (
        <Section title="Size of the biggest changes" subtitle={`${totalsText(d.window.weeks)}; change is latest minus earlier forecast`}>
          <div className={styles.barWrap}>
            <BarChart
              data={bars.map((b) => ({ label: b.label, value: b.value, tone: b.value >= 0 ? "positive" : "negative" }))}
              orientation="horizontal"
              width={560}
              valueFormat={(v) => signedPts(v) ?? "0.0"}
              ariaLabel="Change in projected points for the biggest risers and fallers"
            />
          </div>
        </Section>
      )}

      {mover && <Detail d={d} m={mover} requestedMissing={requestedMissing} />}
      <MoversTable d={d} state={state} sp={sp} />
      <MoversMethod d={d} />
    </div>
  );
}
