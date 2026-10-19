import Link from "next/link";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { EmptyState, Glossary, Notice, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import {
  type CompareResponse, type ParamError, type Side,
  compareHref, isCompareResponse, parseCompareParams, playerName, projectionSourceLine, unknownPlayerSide,
} from "@/lib/compare";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import { EMPTY_RANGES } from "@/lib/projection-ranges";
import { getRanges } from "@/lib/projection-ranges.server";
import { ChartsSection } from "./CompareCharts";
import { ComparePicker, type PickerSelection } from "./ComparePicker";
import { CompareHero, ContextSection, SummarySection } from "./CompareSections";
import { AdvantagesTable, ProjectionTable, UsageTable } from "./CompareTables";
import styles from "./compare.module.css";

export const metadata = { title: "Compare players" };

const TITLE = "Compare players";
const META = "Two players side by side, from projections, usage and scores the app already stores. Nothing here is a forecast of its own.";
const SWITCH_HINT = "Switch between ESPN and Ours with the Proj switch in the top bar (in the menu on tablets and phones).";

const pick = (id: number | null): PickerSelection | null => (id === null ? null : { id, name: null, position: null, team: null });

function fromCard(r: CompareResponse, side: Side): PickerSelection {
  const p = r.players[side === "a" ? 0 : 1].player;
  return { id: p.espn_player_id, name: p.full_name, position: p.position, team: p.pro_team };
}

function Shell({ a, b, meta, children }: { a: PickerSelection | null; b: PickerSelection | null; meta?: ReactNode; children?: ReactNode }) {
  return (
    <div className="stack">
      <PageHeader eyebrow="Players" title={TITLE} meta={meta ?? META}>
        <ComparePicker a={a} b={b} />
      </PageHeader>
      {children}
    </div>
  );
}

export default async function ComparePage({ searchParams }: PageProps<"/compare">) {
  const sp = await searchParams;
  const { a, b, errors } = parseCompareParams(sp);
  const source = await getProjectionSource();
  const sourceLine = projectionSourceLine(source === "ours" ? "independent" : "espn");

  // Junk in the URL: say which side, keep the valid one, and make no backend call.
  if (errors.length > 0) {
    return (
      <Shell a={pick(a)} b={pick(b)}>
        <InvalidIds errors={errors} />
      </Shell>
    );
  }

  if (a === null && b === null) {
    return (
      <Shell a={null} b={null} meta={`${META} ${sourceLine}.`}>
        <Section title="Pick two players">
          <EmptyState title="Nothing to compare yet" action={<Link href="/players">Browse players</Link>}>
            <p>Search for a player in each box above, or open a player&apos;s page and choose Compare to bring them here.</p>
            <p>Kickers and team defenses can be compared too. {SWITCH_HINT}</p>
          </EmptyState>
        </Section>
      </Shell>
    );
  }

  if (a === null || b === null) {
    const one = (a ?? b) as number;
    return (
      <Shell a={pick(a)} b={pick(b)} meta={`${META} ${sourceLine}.`}>
        <Section title="Pick a second player">
          <EmptyState title={`Choose ${a === null ? "Player A" : "Player B"} to start the comparison`} action={<Link href={`/players?id=${one}`}>Open the selected player&apos;s page</Link>}>
            <p>One player is picked. Search for the other in the empty box above.</p>
          </EmptyState>
        </Section>
      </Shell>
    );
  }

  // Exactly one backend call, and only when both ids are valid.
  const res = await api<CompareResponse>(withSource(`/api/compare?a=${a}&b=${b}`, source));

  if (!res.ok) {
    const unknown = res.status === 404 ? unknownPlayerSide(res.error) : null;
    if (res.status === 404) {
      const side = unknown?.side ?? null;
      const id = unknown?.id ?? (side === "a" ? a : side === "b" ? b : null);
      return (
        <Shell a={pick(a)} b={pick(b)}>
          <Section>
            <EmptyState
              title={side ? `Player ${side.toUpperCase()} not found` : "Player not found"}
              action={side ? <Link href={compareHref(side === "a" ? null : a, side === "b" ? null : b)}>Clear Player {side.toUpperCase()}</Link> : <Link href="/compare">Start over</Link>}
            >
              <p>
                {id !== null ? <>No player in this league&apos;s ESPN pool has the id {id}</> : <>One of the ids is not in this league&apos;s ESPN pool</>}
                {side ? <> (Player {side.toUpperCase()})</> : null}. Pick a different player in the box above.
              </p>
            </EmptyState>
          </Section>
        </Shell>
      );
    }
    if (res.status === 422) {
      return (
        <Shell a={pick(a)} b={pick(b)}>
          <Notice tone="caution" title="The comparison could not be built">{res.error}</Notice>
        </Shell>
      );
    }
    return (
      <Shell a={pick(a)} b={pick(b)}>
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Shell>
    );
  }

  if (!isCompareResponse(res.data)) {
    return (
      <Shell a={pick(a)} b={pick(b)}>
        <Notice tone="negative" title="Unexpected response">
          The backend answered, but not with a comparison this page can read. Reload; if it persists, the backend and frontend may be out of step.
        </Notice>
      </Shell>
    );
  }

  const r = res.data;
  const ranges = r.source === "independent" ? await getRanges(r.as_of_week) : EMPTY_RANGES;
  const pa = fromCard(r, "a");
  const pb = fromCard(r, "b");
  const meta = (
    <>
      {projectionSourceLine(r.source)} · week {r.as_of_week} projections · usage and points through week {r.completed_through_week}
      {" "}· scoring: {r.scoring.label}.{" "}
      <span className={styles.switchHint}>{SWITCH_HINT}</span>
    </>
  );

  if (r.identical) {
    return (
      <Shell a={pa} b={pb} meta={meta}>
        <Notice tone="info" title="Same player on both sides">
          {playerName(r.players[0])} is picked twice, so there is nothing to compare. Change Player B above to choose a different player.
        </Notice>
        <CompareHero r={r} only="a" />
        <SummarySection r={r} />
      </Shell>
    );
  }

  return (
    <Shell a={pa} b={pb} meta={meta}>
      <CompareHero r={r} />
      {!r.same_position && (
        <Notice tone="caution" title="Different positions">
          {r.players[0].player.position ?? "?"} and {r.players[1].player.position ?? "?"} do not share every measure. Rows that mean nothing for a
          position show why instead of a number, and ratings are percentiles within each position, so they are not compared across positions.
        </Notice>
      )}
      <SummarySection r={r} />
      <Section
        title="Projection comparison"
        subtitle={`${projectionSourceLine(r.source)}. ${r.scoring.note ?? ""}`.trim()}
        flush
      >
        <ProjectionTable r={r} ranges={ranges} />
      </Section>
      <Section
        title="Usage comparison"
        subtitle={`nflverse usage through week ${r.completed_through_week}. Expected PPR is what an average player would score from the same targets, carries and field position.`}
        flush
      >
        <UsageTable r={r} />
      </Section>
      <ContextSection r={r} />
      <ChartsSection r={r} />
      <Section title="Statistical advantages" subtitle="Every measure in one compact table. An edge appears only when the gap passes a materiality threshold." flush>
        <AdvantagesTable r={r} />
      </Section>
      <Glossary terms={["Projected points", "PPR/g", "xFP", "Snap share", "Target share", "ROS", "Value rating", "—"]} />
    </Shell>
  );
}

function InvalidIds({ errors }: { errors: ParamError[] }) {
  return (
    <Notice tone="caution" title={errors.length === 1 ? "That player id isn't valid" : "Those player ids aren't valid"}>
      <ul className={styles.errorList}>{errors.map((e) => <li key={e.side}>{e.message}</li>)}</ul>
      <p>Pick the player again in the box above, or <Link href="/compare">start over</Link>. A player on the other side is kept.</p>
    </Notice>
  );
}
